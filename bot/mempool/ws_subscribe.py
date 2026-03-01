# bot/mempool/ws_subscribe.py
import asyncio, json, random, urllib.parse, logging, os, time
import websockets
from typing import AsyncIterator, Dict, Any, List, Optional
from bot.config.knobs import Knobs as K
from bot.core.chain_config import get_chain_config
from bot.net.rpc_client import RpcClient
from bot.core.telemetry import (
    canonical_metric_labels,
    get_endpoint_labels,
    mempool_rx_total,
    mempool_rx_errors_total,
    mempool_reconnects_total,
    mempool_ws_connected,
    mempool_stream_publish_total,
    mempool_stream_publish_errors_total,
)
from redis.asyncio import Redis

log = logging.getLogger("ws-sub")
_CHAIN_METRIC_LABELS = canonical_metric_labels()

def _payload_for(endpoint: str) -> Dict[str, Any]:
    host = (urllib.parse.urlparse(endpoint).hostname or "").lower()
    if "alchemy.com" in host:
        return {"jsonrpc":"2.0","id":1,"method":"alchemy_subscribe",
                "params":["alchemy_pendingTransactions", {}]}
    return {"jsonrpc":"2.0","id":1,"method":"eth_subscribe",
            "params":["newPendingTransactions"]}

async def _connect_and_subscribe(endpoint: str):
    ws = await websockets.connect(endpoint, ping_interval=30)
    await ws.send(json.dumps(_payload_for(endpoint)))
    resp = await ws.recv()
    if '"error"' in resp:
        raise RuntimeError(f"subscribe failed: {resp}")
    return ws

async def pending_stream(endpoints: List[str], on_connect=None) -> AsyncIterator[Dict[str, Any]]:
    """Yields dicts with either {'hash': '0x...'} or {'tx': full_tx_dict}."""
    idx, backoff, retries = 0, 0.5, 0
    while True:
        ep = endpoints[idx % len(endpoints)]
        try:
            ws = await _connect_and_subscribe(ep)
            log.info("connected endpoint=%s", ep)
            ep_labels = get_endpoint_labels(ep)
            mempool_ws_connected.labels(**ep_labels).set(1)
            if on_connect:
                try:
                    await on_connect(ep)
                except Exception:
                    pass
            backoff = 0.5
            retries = 0
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    params = msg.get("params", {})
                    res = params.get("result")
                    if isinstance(res, str) and res.startswith("0x"):
                        mempool_rx_total.labels(**ep_labels).inc()
                        yield {"hash": res}
                    elif isinstance(res, dict):
                        mempool_rx_total.labels(**ep_labels).inc()
                        yield {"tx": res}
                except Exception:
                    continue
        except Exception as e:
            ep_labels = get_endpoint_labels(ep)
            mempool_ws_connected.labels(**ep_labels).set(0)
            mempool_rx_errors_total.labels(**ep_labels).inc()
            mempool_reconnects_total.labels(**ep_labels).inc()
            retries += 1
            low = backoff * 0.8
            high = backoff * 1.2
            delay = random.uniform(low, high)
            log.warning(
                "ws error on %s: %s; retry=%d next_delay_s=%.2f",
                ep,
                e,
                retries,
                delay,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 60.0)
            idx += 1
            continue

async def ingest_to_queue(redis_client, stream_name: str, endpoints: List[str], rpc: Optional[RpcClient]=None):
    """
    Reads pending events, applies sampling + filters, writes to Redis stream.
    """
    rpc = rpc or RpcClient()
    async def _on_connect(ep: str):
        await redis_client.hset("mempool:producer", mapping={"endpoint": ep, "ts_ms": str(int(time.time()*1000))})

    async for ev in pending_stream(endpoints, on_connect=_on_connect):
        # ---- SAMPLE before any HTTP
        if random.random() > K.PENDING_SAMPLE_RATE:
            # dropped by sampling
            continue

        tx = None
        if "tx" in ev:
            tx = ev["tx"]                       # full object (Alchemy stream)
        else:
            # hash-only -> minimal HTTP lookup (rate-limited)
            tx = await rpc.get_tx(ev["hash"])

        if not tx:
            continue

        # ---- FILTERS
        # MIN VALUE
        try:
            val = int(tx.get("value", "0"), 0) if isinstance(tx.get("value"), str) else int(tx.get("value", 0))
        except Exception:
            val = 0
        if val < K.MIN_VALUE_WEI:
            continue

        # METHOD SELECTOR
        inp = tx.get("input") or tx.get("data") or ""
        selector = (inp[:10] or "").lower()
        if K.ALLOW_METHOD_IDS and selector not in K.ALLOW_METHOD_IDS:
            continue

        # ---- Emit to Redis stream (lean payload)
        payload = {
            "hash": (tx.get("hash") or ev.get("hash") or "").lower(),
            "from": (tx.get("from") or "").lower(),
            "to": (tx.get("to") or "").lower(),
            "selector": selector,
            "value": str(val),
            "nonce": str(tx.get("nonce", "")),
            "chain_id": str(K.CHAIN_ID),
            "ts_ms": str(int(asyncio.get_event_loop().time()*1000)),
        }
        try:
            await redis_client.xadd(stream_name, payload, maxlen=50_000, approximate=True)
            mempool_stream_publish_total.labels(**_CHAIN_METRIC_LABELS, stream=stream_name).inc()
        except Exception as e:
            log.warning("redis xadd failed: %s", e)
            mempool_stream_publish_errors_total.labels(**_CHAIN_METRIC_LABELS, stream=stream_name).inc()
            await asyncio.sleep(0.05)

async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    cfg = get_chain_config()
    if not cfg.ws_endpoints:
        raise SystemExit("WS_ENDPOINTS is empty")
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    stream = os.getenv("REDIS_STREAM", "mempool:pending:txs")
    log.info("producer start: chain=%s redis=%s stream=%s endpoints=%s", cfg.chain, redis_url, stream, cfg.ws_endpoints)
    r = Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        await ingest_to_queue(r, stream, cfg.ws_endpoints, RpcClient())
    finally:
        close = getattr(r, "aclose", None) or getattr(r, "close", None)
        if close:
            res = close()
            if asyncio.iscoroutine(res):
                await res

if __name__ == "__main__":
    asyncio.run(main())

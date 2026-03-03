# bot/workers/mempool_consumer.py
from __future__ import annotations

import os
import asyncio
import time
import logging
import contextlib
from collections import deque
from typing import Dict, Any, Tuple, List

import aiohttp
from redis.asyncio import Redis
from bot.core.chain_config import get_chain_config
from bot.storage.pg import (
    get_pool,
    ensure_mempool_samples_table,
    ensure_mempool_pipeline_tables,
    upsert_mempool_sample,
    insert_mempool_event,
    upsert_mempool_tx,
    insert_mempool_error,
)

from bot.core.telemetry import (
    canonical_metric_labels,
    get_endpoint_labels,
    mempool_stream_consume_total,
    mempool_stream_consume_errors_total,
    mempool_consumer_throughput_tps,
    mempool_stream_consume_lag_ms,
    mempool_stream_consume_lag_ms_legacy,
    mempool_stream_xlen,
    mempool_stream_group_lag,
    mempool_dlq_writes_total,
    mempool_tps,
    mempool_tpm,
    mempool_tps_legacy,
    mempool_tpm_legacy,
    rpc_gettx_ok_total,
    rpc_gettx_errors_total,
    rpc_gettx_429_total,
    rpc_rate_limit_waits_total,
    rpc_circuit_breaker_trips_total,
    rpc_circuit_breaker_open,
    rpc_429_ratio,
    dex_tx_detected_total,
)

# --- Config ---
STREAM        = os.getenv("REDIS_STREAM",   "mempool:pending:txs")
GROUP         = os.getenv("REDIS_GROUP",    "mempool")
CONSUMER      = os.getenv("REDIS_CONSUMER") or os.getenv("HOSTNAME") or "worker-1"
REDIS_URL     = os.getenv("REDIS_URL",      "redis://redis:6379/0")

_CHAIN_CFG = get_chain_config()
_CHAIN_LABELS = canonical_metric_labels(chain=_CHAIN_CFG.chain, chain_family=os.getenv("CHAIN_FAMILY", "evm"))
RPC_URLS = [_CHAIN_CFG.rpc_http] + _CHAIN_CFG.rpc_http_backups
_RPC_IDX = 0

CONCURRENCY   = int(os.getenv("MEMPOOL_CONCURRENCY", "10"))
RECLAIM_IDLE  = int(os.getenv("MEMPOOL_RECLAIM_IDLE_MS", "60000"))   # 60s
RECLAIM_SLEEP = float(os.getenv("MEMPOOL_RECLAIM_SLEEP_SEC", "5"))   # 5s
RECLAIM_IDLE_MS  = int(os.getenv("MEMPOOL_RECLAIM_IDLE_MS",  "60000"))  # 60s
RECLAIM_EVERY_MS = int(os.getenv("MEMPOOL_RECLAIM_EVERY_MS", "5000"))   # 5s
RPC_RPS = float(os.getenv("RPC_RPS", "8"))
RPC_BURST = int(os.getenv("RPC_BURST", "16"))
RPC_429_WINDOW = int(os.getenv("RPC_429_WINDOW", "100"))
RPC_429_THRESHOLD = float(os.getenv("RPC_429_THRESHOLD", "0.30"))
RPC_429_PAUSE_SEC = float(os.getenv("RPC_429_PAUSE_SEC", "15"))

DLQ_STREAM    = os.getenv("REDIS_DLQ_STREAM", "mempool:dlq")
DLQ_MAXLEN    = int(os.getenv("REDIS_DLQ_MAXLEN", "100000"))
REPORT_EVERY_SEC = float(os.getenv("MEMPOOL_REPORT_EVERY_SEC", "15"))

# Known DEX routers (lowercased)
DEFAULT_ROUTERS = {
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # UniV3 SwapRouter
    "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506",  # Sushi
    "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff",  # QuickSwap
}
DEX_ROUTERS = {
    a.strip().lower() for a in os.getenv("DEX_ROUTERS", "").split(",") if a.strip()
} or DEFAULT_ROUTERS

log = logging.getLogger("mempool-consumer")
_NotSet = object()
_stats = {
    "processed": 0,
    "failed_fetch": 0,
    "process_errors": 0,
    "xread_errors": 0,
    "dlq_ok": 0,
    "dlq_err": 0,
    "lag_sum_ms": 0.0,
    "lag_count": 0,
    "lag_max_ms": 0.0,
    "rpc429": 0,
    "circuit_open_s": 0.0,
    "circuit_trips": 0,
}
_circuit_open_until = 0.0
_rpc_outcomes = deque(maxlen=max(1, RPC_429_WINDOW))


class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = max(0.1, float(rate))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        async with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self.updated_at)
            self.updated_at = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            need = 1.0 - self.tokens
            wait_s = need / self.rate
            self.tokens = 0.0
            self.updated_at = now + wait_s
            return wait_s


_rpc_bucket = TokenBucket(RPC_RPS, RPC_BURST)

# --- Redis bootstrap ---
async def ensure_group(r: Redis) -> None:
    """Create stream+group if needed; start tailing new entries ($)."""
    try:
        await r.xgroup_create(name=STREAM, groupname=GROUP, id="$", mkstream=True)
        log.info("Created stream/group %s/%s at $", STREAM, GROUP)
    except Exception as e:
        # BUSYGROUP or already created
        if "BUSYGROUP" in str(e):
            log.info("Group %s already exists on %s", GROUP, STREAM)
        else:
            log.debug("xgroup_create notice: %s", e)

# --- Helpers ---
def _field(fields: Dict[Any, Any], *names: str, default=_NotSet, cast=lambda x: x):
    """Fetch by any of the given names, handling bytes keys/values."""
    for n in names:
        k = n if n in fields else n.encode()
        if k in fields:
            v = fields[k]
            if isinstance(v, (bytes, bytearray)):
                v = v.decode()
            try:
                return cast(v)
            except Exception:
                pass
    if default is _NotSet:
        raise KeyError(names)
    return default

def _ts_ms_from_entry_id(entry_id: str) -> int | None:
    try:
        return int(entry_id.split("-")[0])
    except Exception:
        return None

def _parse_entry(fields: Dict[Any, Any], entry_id: str) -> Tuple[str, int]:
    """Return (tx_hash, ts_ms). Accept {tx|hash} and {ts_ms|ts} (sec)."""
    txh = _field(fields, "tx", "hash", cast=str)
    ts_ms = _field(fields, "ts_ms", default=None, cast=int)
    if ts_ms is None:
        ts = _field(fields, "ts", default=None, cast=float)
        if ts is not None:
            ts_ms = int(float(ts) * 1000.0)
    if ts_ms is None:
        ts_ms = _ts_ms_from_entry_id(entry_id) or int(time.time() * 1000)
    return txh, ts_ms

def _decode_fields(fields: Dict[Any, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in fields.items():
        key = k.decode(errors="ignore") if isinstance(k, (bytes, bytearray)) else str(k)
        if isinstance(v, (bytes, bytearray)):
            out[key] = v.decode(errors="ignore")
        else:
            out[key] = v
    return out

async def fetch_tx(sess: aiohttp.ClientSession, tx_hash: str) -> Tuple[Dict[str, Any] | None, str | None, str | None, int | None, str | None]:
    global _RPC_IDX, _circuit_open_until
    if not RPC_URLS:
        return None, "no_rpc", None, None, "No RPC endpoints configured"
    now = time.monotonic()
    if now < _circuit_open_until:
        return None, "circuit_open", None, None, "RPC circuit breaker open"

    wait_s = await _rpc_bucket.acquire()
    if wait_s > 0:
        rpc_rate_limit_waits_total.inc()
        await asyncio.sleep(wait_s)

    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx_hash]}
    backoff = 0.2
    for i in range(len(RPC_URLS)):
        url = RPC_URLS[(_RPC_IDX + i) % len(RPC_URLS)]
        try:
            async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 429:
                    rpc_gettx_429_total.inc()
                    _stats["rpc429"] += 1
                    _rpc_outcomes.append(1)
                    ratio = float(sum(_rpc_outcomes)) / float(len(_rpc_outcomes))
                    rpc_429_ratio.set(ratio)
                    if len(_rpc_outcomes) >= max(10, RPC_429_WINDOW // 2) and ratio > RPC_429_THRESHOLD:
                        window_len = len(_rpc_outcomes)
                        _circuit_open_until = time.monotonic() + RPC_429_PAUSE_SEC
                        _stats["circuit_trips"] += 1
                        _stats["circuit_open_s"] = RPC_429_PAUSE_SEC
                        rpc_circuit_breaker_trips_total.inc()
                        rpc_circuit_breaker_open.set(1)
                        _rpc_outcomes.clear()
                        log.warning(
                            "rpc circuit breaker opened ratio=%.3f threshold=%.3f window=%d pause_s=%.1f",
                            ratio,
                            RPC_429_THRESHOLD,
                            window_len,
                            RPC_429_PAUSE_SEC,
                        )
                    await asyncio.sleep(backoff + 0.1 * i)
                    backoff = min(backoff * 2, 2.0)
                    return None, "rate_limited", url, 429, "HTTP 429"
                _rpc_outcomes.append(0)
                ratio = float(sum(_rpc_outcomes)) / float(len(_rpc_outcomes))
                rpc_429_ratio.set(ratio)
                data = await resp.json()
                result = data.get("result")
                if result:
                    _RPC_IDX = (_RPC_IDX + 1) % len(RPC_URLS)
                    rpc_gettx_ok_total.inc()
                    log.debug("fetch_tx ok hash=%s to=%s", tx_hash, result.get("to"))
                    return result, None, url, resp.status, None
                msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else "no_result"
                return None, "no_result", url, resp.status, str(msg)
        except Exception as e:
            rpc_gettx_errors_total.inc()
            log.debug("RPC error for %s: %s", tx_hash, e)
            return None, "rpc_exception", url, None, str(e)
    return None, "no_result", RPC_URLS[0], None, "no result"

def looks_like_dex(tx: Dict[str, Any]) -> bool:
    to = tx.get("to")
    return isinstance(to, str) and to.lower() in DEX_ROUTERS

def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            return int(v, 0)
        return int(v)
    except Exception:
        return None

async def _process_one(
    r: Redis,
    sess: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    pool,
    stream_key: str,
    entry_id: str,
    fields: Dict[Any, Any],
):
    async with sem:
        try:
            txh, ts_ms = _parse_entry(fields, entry_id)
            decoded_fields = _decode_fields(fields)
            source_endpoint = decoded_fields.get("endpoint") or decoded_fields.get("source_endpoint")

            await insert_mempool_event(
                pool,
                ts_ms=int(ts_ms),
                tx_hash=txh,
                source_endpoint=source_endpoint,
                stream_id=str(entry_id),
                raw_json=decoded_fields,
            )
            await upsert_mempool_tx(
                pool,
                {
                    "tx_hash": txh,
                    "chain_id": _CHAIN_CFG.chain_id,
                    "from": decoded_fields.get("from"),
                    "to": decoded_fields.get("to"),
                    "nonce": _to_int(decoded_fields.get("nonce")),
                    "gas": _to_int(decoded_fields.get("gas")),
                    "max_fee": _to_int(decoded_fields.get("maxFeePerGas") or decoded_fields.get("max_fee")),
                    "max_priority": _to_int(decoded_fields.get("maxPriorityFeePerGas") or decoded_fields.get("max_priority")),
                    "value": _to_int(decoded_fields.get("value")),
                    "input_len": len(decoded_fields.get("input")) if isinstance(decoded_fields.get("input"), str) else None,
                    "first_seen_ts_ms": int(ts_ms),
                    "last_seen_ts_ms": int(ts_ms),
                },
            )

            lag_ms = max(0.0, time.time() * 1000.0 - float(ts_ms))
            mempool_stream_consume_total.labels(**_CHAIN_LABELS, stream=STREAM).inc()
            endpoint_label = source_endpoint or "unknown"
            endpoint_labels = get_endpoint_labels(endpoint_label)
            mempool_stream_consume_lag_ms.labels(**endpoint_labels).observe(lag_ms)
            mempool_stream_consume_lag_ms_legacy.labels(**endpoint_labels).observe(lag_ms)
            _stats["processed"] += 1
            _stats["lag_sum_ms"] += lag_ms
            _stats["lag_count"] += 1
            _stats["lag_max_ms"] = max(_stats["lag_max_ms"], lag_ms)

            tx, err_type, endpoint, http_status, err_msg = await fetch_tx(sess, txh)
            if tx and looks_like_dex(tx):
                dex_tx_detected_total.inc()
                # TODO: forward to pipeline

            if tx:
                tx_input = tx.get("input") or tx.get("data") or ""
                await upsert_mempool_tx(
                    pool,
                    {
                        "tx_hash": txh,
                        "chain_id": _CHAIN_CFG.chain_id,
                        "from": tx.get("from"),
                        "to": tx.get("to"),
                        "nonce": _to_int(tx.get("nonce")),
                        "gas": _to_int(tx.get("gas")),
                        "max_fee": _to_int(tx.get("maxFeePerGas")),
                        "max_priority": _to_int(tx.get("maxPriorityFeePerGas")),
                        "value": _to_int(tx.get("value")),
                        "input_len": len(tx_input) if isinstance(tx_input, str) else None,
                        "first_seen_ts_ms": int(ts_ms),
                        "last_seen_ts_ms": int(ts_ms),
                    },
                )
                row = {
                    "ts_ms": int(ts_ms),
                    "tx_hash": txh,
                    "to_addr": tx.get("to"),
                    "from_addr": tx.get("from"),
                    "value": _to_int(tx.get("value")),
                    "gas": _to_int(tx.get("gas")),
                    "max_fee_per_gas": _to_int(tx.get("maxFeePerGas")),
                    "max_priority_fee_per_gas": _to_int(tx.get("maxPriorityFeePerGas")),
                    "status": "fetched",
                    "error": None,
                }
                await upsert_mempool_sample(pool, row)
            else:
                await insert_mempool_error(
                    pool,
                    ts_ms=int(ts_ms),
                    tx_hash=txh,
                    endpoint=endpoint,
                    error_type=err_type or "unknown",
                    error_msg=err_msg or err_type or "unknown",
                    http_status=http_status,
                )
                row = {
                    "ts_ms": int(ts_ms),
                    "tx_hash": txh,
                    "status": "failed",
                    "error": err_type or "unknown",
                }
                _stats["failed_fetch"] += 1
                mempool_stream_consume_errors_total.labels(**_CHAIN_LABELS, stream=STREAM, kind="fetch_failed").inc()
                await upsert_mempool_sample(pool, row)

        except Exception as e:
            # push to DLQ but never crash
            _stats["process_errors"] += 1
            mempool_stream_consume_errors_total.labels(**_CHAIN_LABELS, stream=STREAM, kind="process_exception").inc()
            try:
                dlq_fields = {str(k): str(v) for k, v in fields.items()}
                dlq_fields["err"] = str(e)
                await r.xadd(DLQ_STREAM, dlq_fields, maxlen=DLQ_MAXLEN, approximate=True)
                _stats["dlq_ok"] += 1
                mempool_dlq_writes_total.labels(
                    **_CHAIN_LABELS, stream=STREAM, dlq_stream=DLQ_STREAM, result="ok"
                ).inc()
                log.debug("process exception sent to DLQ tx=%s err=%s", fields.get("tx") or fields.get(b"tx"), e)
            except Exception as dlq_err:
                _stats["dlq_err"] += 1
                mempool_dlq_writes_total.labels(
                    **_CHAIN_LABELS, stream=STREAM, dlq_stream=DLQ_STREAM, result="error"
                ).inc()
                log.warning("DLQ write failed entry_id=%s err=%s", entry_id, dlq_err)
        finally:
            with contextlib.suppress(Exception):
                await r.xack(stream_key, GROUP, entry_id)

async def _consume_loop(r: Redis, sess: aiohttp.ClientSession, sem: asyncio.Semaphore, pool):
    while True:
        try:
            entries: List[Tuple[bytes, List[Tuple[str, Dict[Any, Any]]]]] = await r.xreadgroup(
                groupname=GROUP,
                consumername=CONSUMER,
                streams={STREAM: ">"},
                count=100,
                block=1000,  # 1s
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _stats["xread_errors"] += 1
            mempool_stream_consume_errors_total.labels(**_CHAIN_LABELS, stream=STREAM, kind="xreadgroup").inc()
            log.warning("xreadgroup error: %s", e)
            await asyncio.sleep(1)
            continue

        if not entries:
            continue

        tasks: List[asyncio.Task] = []
        for stream_key, items in entries:
            for entry_id, fields in items:
                tasks.append(asyncio.create_task(_process_one(r, sess, sem, pool, stream_key, entry_id, fields)))

        if tasks:
            # swallow per-item exceptions; they go to DLQ
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)

async def _reclaim_loop(r: Redis):
    next_id = "0-0"
    while True:
        try:
            # Prefer positional args: some redis-py builds reject 'start='
            resp = await r.xautoclaim(
                STREAM,          # name
                GROUP,           # groupname
                CONSUMER,        # consumername
                RECLAIM_IDLE_MS, # min_idle_time (ms)
                next_id,         # start
                count=100,
            )
        except TypeError:
            # Fallback for variants expecting start_id=
            resp = await r.xautoclaim(
                name=STREAM,
                groupname=GROUP,
                consumername=CONSUMER,
                min_idle_time=RECLAIM_IDLE_MS,
                start_id=next_id,
                count=100,
            )

        # 2-tuple vs 3-tuple compatibility
        if isinstance(resp, tuple):
            if len(resp) == 2:
                next_id, items = resp
            elif len(resp) == 3:
                next_id, items, _deleted = resp
            else:
                log.warning("Unexpected XAUTOCLAIM response: %r", resp)
                await asyncio.sleep(RECLAIM_EVERY_MS / 1000.0)
                continue
        else:
            try:
                next_id, items = resp[0], resp[1]
            except Exception:
                log.warning("Unrecognized XAUTOCLAIM response: %r", resp)
                await asyncio.sleep(RECLAIM_EVERY_MS / 1000.0)
                continue

        if items:
            ids = [eid for (eid, _fields) in items]
            if ids:
                try:
                    await r.xack(STREAM, GROUP, *ids)
                except Exception as e:
                    log.warning("XACK failed for %s: %s", ids[:3], e)

        await asyncio.sleep(RECLAIM_EVERY_MS / 1000.0)

def _decode_redis_obj(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode(errors="ignore")
    return v


async def _report_loop(r: Redis):
    last_t = time.time()
    last_processed = 0
    while True:
        await asyncio.sleep(REPORT_EVERY_SEC)
        now = time.time()
        window_s = max(0.001, now - last_t)
        processed = int(_stats["processed"])
        delta = max(0, processed - last_processed)
        tps = float(delta) / window_s
        mempool_consumer_throughput_tps.labels(**_CHAIN_LABELS, stream=STREAM, consumer=CONSUMER).set(tps)
        mempool_tps.labels(**_CHAIN_LABELS).set(tps)
        mempool_tpm.labels(**_CHAIN_LABELS).set(tps * 60.0)
        mempool_tps_legacy.labels(**_CHAIN_LABELS).set(tps)
        mempool_tpm_legacy.labels(**_CHAIN_LABELS).set(tps * 60.0)

        lag_count = int(_stats["lag_count"])
        avg_lag = (_stats["lag_sum_ms"] / lag_count) if lag_count else 0.0
        max_lag = float(_stats["lag_max_ms"])
        failed_fetch = int(_stats["failed_fetch"])
        process_errors = int(_stats["process_errors"])
        xread_errors = int(_stats["xread_errors"])
        dlq_ok = int(_stats["dlq_ok"])
        dlq_err = int(_stats["dlq_err"])
        rpc429 = int(_stats["rpc429"])
        circuit_trips = int(_stats["circuit_trips"])
        circuit_open_s = max(0.0, _circuit_open_until - time.monotonic())
        _stats["circuit_open_s"] = circuit_open_s
        rpc_circuit_breaker_open.set(1 if circuit_open_s > 0 else 0)
        ratio = float(sum(_rpc_outcomes)) / float(len(_rpc_outcomes)) if _rpc_outcomes else 0.0

        with contextlib.suppress(Exception):
            xlen = await r.xlen(STREAM)
            mempool_stream_xlen.labels(**_CHAIN_LABELS, stream=STREAM).set(float(xlen))
        with contextlib.suppress(Exception):
            groups = await r.xinfo_groups(STREAM)
            for g in groups:
                name = str(_decode_redis_obj(g.get("name", "")))
                if name != GROUP:
                    continue
                lag = float(int(_decode_redis_obj(g.get("lag", 0)) or 0))
                mempool_stream_group_lag.labels(**_CHAIN_LABELS, stream=STREAM, group=GROUP).set(lag)
                break

        log.info(
            "consumer_stats window_s=%.1f processed=%d tps=%.2f avg_lag_ms=%.1f max_lag_ms=%.1f failed_fetch=%d process_errors=%d xread_errors=%d dlq_ok=%d dlq_err=%d rpc429=%d rpc429_ratio=%.3f circuit_open_s=%.1f circuit_trips=%d",
            window_s,
            delta,
            tps,
            avg_lag,
            max_lag,
            failed_fetch,
            process_errors,
            xread_errors,
            dlq_ok,
            dlq_err,
            rpc429,
            ratio,
            circuit_open_s,
            circuit_trips,
        )

        _stats["failed_fetch"] = 0
        _stats["process_errors"] = 0
        _stats["xread_errors"] = 0
        _stats["dlq_ok"] = 0
        _stats["dlq_err"] = 0
        _stats["lag_sum_ms"] = 0.0
        _stats["lag_count"] = 0
        _stats["lag_max_ms"] = 0.0
        _stats["rpc429"] = 0

        last_t = now
        last_processed = processed


async def run_consumer():
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level)
    rpc_circuit_breaker_open.set(0)
    rpc_429_ratio.set(0)
    log.info(
        "Starting mempool_consumer: stream=%s group=%s consumer=%s rpc_rps=%.2f rpc_burst=%d rpc_429_window=%d rpc_429_threshold=%.3f rpc_429_pause_sec=%.1f rpc_endpoints=%s",
        STREAM,
        GROUP,
        CONSUMER,
        RPC_RPS,
        RPC_BURST,
        RPC_429_WINDOW,
        RPC_429_THRESHOLD,
        RPC_429_PAUSE_SEC,
        RPC_URLS,
    )

    r = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=False)
    await ensure_group(r)
    pool = await get_pool()
    await ensure_mempool_samples_table(pool)
    await ensure_mempool_pipeline_tables(pool)

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(keepalive_timeout=30, ttl_dns_cache=60)
    timeout = aiohttp.ClientTimeout(total=8)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
        consume_task = asyncio.create_task(_consume_loop(r, sess, sem, pool))
        reclaim_task = asyncio.create_task(_reclaim_loop(r))
        report_task = asyncio.create_task(_report_loop(r))
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(consume_task, reclaim_task, report_task)
        finally:
            report_task.cancel()
            reclaim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaim_task
            with contextlib.suppress(asyncio.CancelledError):
                await report_task
            # Close Redis cleanly across redis-py versions
            close = getattr(r, "aclose", None) or getattr(r, "close", None)
            if close:
                res = close()
                if asyncio.iscoroutine(res):
                    with contextlib.suppress(Exception):
                        await res
            await pool.close()


if __name__ == "__main__":
    asyncio.run(run_consumer())

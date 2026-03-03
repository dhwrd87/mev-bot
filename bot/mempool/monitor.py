from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

import websockets  # patched by tests or real WS

logger = logging.getLogger("bot.mempool.monitor")

# metrics are optional; fall back to no-ops if not wired
try:
    from bot.metrics import mempool_unique_tx_total
except Exception:  # pragma: no cover
    class _Noop:
        def inc(self, *a, **k): pass
    mempool_unique_tx_total = _Noop()

try:
    from bot.core.telemetry import canonical_metric_labels

    _CHAIN_METRIC_LABELS = canonical_metric_labels()
except Exception:  # pragma: no cover
    _CHAIN_METRIC_LABELS = {}

# --- helpers ---------------------------------------------------------------

def _get_ws_endpoints_from_env() -> List[str]:
    from bot.core.chain_config import get_chain_config
    return get_chain_config().ws_endpoints

def _subscribe_payload_for(endpoint: str) -> str:
    host = (urlparse(endpoint).hostname or "").lower()
    if "alchemy.com" in host:
        return json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_subscribe",
            "params": ["alchemy_pendingTransactions", {}],
        })
    return json.dumps(SUBSCRIBE_MSG)


SUBSCRIBE_MSG = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "eth_subscribe",
    "params": ["newPendingTransactions"],
}

# --- class -----------------------------------------------------------------

try:  # optional redis
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None

class WSMempoolMonitor:
    def __init__(self, endpoints: Optional[List[str]] = None, metrics_port: Optional[int] = None, **kwargs):
        # prefer env, but allow explicit list
        self.endpoints: List[str] = _get_ws_endpoints_from_env() or (endpoints or [])
        if not self.endpoints:
            logger.warning("No WS endpoints set for chain=%s; mempool monitor not started.", os.getenv("CHAIN"))
            self.enabled = False
        else:
            logger.info("WSMempoolMonitor starting with %d endpoints: %s", len(self.endpoints), self.endpoints)
            self.enabled = True

        self.metrics_port = metrics_port
        self._stop = asyncio.Event()

        # queues + dedup
        self._queue: asyncio.Queue[Tuple[str, float]] = asyncio.Queue(maxsize=10_000)
        self._seen: Set[str] = set()
        self._seen_order: Deque[str] = deque(maxlen=100_000)
        self._timestamps: Deque[float] = deque()
        self._tasks: List[asyncio.Task] = []

        # redis wiring (best-effort)
        self.redis_stream: Optional[str] = kwargs.get("redis_stream") or os.getenv("REDIS_STREAM") or "mempool:pending:txs"
        self.redis_url: str = kwargs.get("redis_url") or os.getenv("REDIS_URL") or "redis://redis:6379/0"
        self.redis_maxlen: int = int(kwargs.get("redis_maxlen", os.getenv("REDIS_MAXLEN", "100000")))
        self._redis = None
        self.connected_endpoint: Optional[str] = None

    async def start(self) -> "WSMempoolMonitor":
        if not self.enabled:
            return self

        if self.redis_stream and aioredis is not None:
            try:
                self._redis = aioredis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
                logger.info("Redis stream enabled: %s @ %s", self.redis_stream, self.redis_url)
            except Exception as e:
                logger.warning("Redis init failed (%s); continuing without stream.", e)
                self._redis = None

        loop = asyncio.get_running_loop()
        # start readers
        for ep in self.endpoints:
            self._tasks.append(loop.create_task(self._reader(ep)))
        # start aggregator
        self._tasks.append(loop.create_task(self._aggregator()))
        await asyncio.sleep(0)  # yield
        return self

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            try:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            except Exception:
                pass
        self._tasks.clear()
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass

    # --- internals ---------------------------------------------------------

    async def _reader(self, endpoint: str) -> None:
        """Connect, subscribe, then stream pending hashes with backoff."""
        backoff = 0.05
        while not self._stop.is_set():
            try:
                conn = websockets.connect(endpoint, ping_interval=30)
                if hasattr(conn, "__aenter__"):
                    async with conn as ws:
                        await self._handle_ws(ws, endpoint)
                elif inspect.isawaitable(conn):
                    ws = await conn
                    try:
                        await self._handle_ws(ws, endpoint)
                    finally:
                        close = getattr(ws, "close", None)
                        if close:
                            await close()
                else:
                    ws = conn
                    await self._handle_ws(ws, endpoint)
            except Exception as e:
                logger.warning("WS reader error on %s: %s; reconnect in %.1fs", endpoint, e, backoff)
                if self.connected_endpoint == endpoint:
                    self.connected_endpoint = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)

    async def _handle_ws(self, ws, endpoint: str) -> None:
        payload = _subscribe_payload_for(endpoint)
        try:
            await ws.send(payload)
            ack = await asyncio.wait_for(ws.recv(), timeout=5)
            self.connected_endpoint = endpoint
            logger.info("WS subscribe ack on %s: %s", endpoint, str(ack)[:140])
            for h in self._extract_hashes(ack):
                try:
                    self._queue.put_nowait((h, time.time()))
                except asyncio.QueueFull:
                    pass
        except Exception as e:
            logger.warning("No subscribe ack on %s (%s)", endpoint, e)
            if not isinstance(e, asyncio.TimeoutError):
                raise

        # read loop
        while not self._stop.is_set():
            raw = await ws.recv()
            ts = time.time()
            for h in self._extract_hashes(raw):
                try:
                    self._queue.put_nowait((h, ts))
                except asyncio.QueueFull:
                    pass

    async def _aggregator(self) -> None:
        """Deduplicate, bump metrics, and publish to Redis stream (if enabled)."""
        while not self._stop.is_set():
            try:
                tx_hash, ts_recv = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

            if tx_hash in self._seen:
                continue

            self._seen.add(tx_hash)
            self._seen_order.append(tx_hash)
            if len(self._seen) == self._seen_order.maxlen:
                for _ in range(self._seen_order.maxlen // 2):
                    old = self._seen_order.popleft()
                    self._seen.discard(old)

            self._timestamps.append(ts_recv)
            try:
                if _CHAIN_METRIC_LABELS and hasattr(mempool_unique_tx_total, "labels"):
                    mempool_unique_tx_total.labels(**_CHAIN_METRIC_LABELS).inc()
                else:
                    mempool_unique_tx_total.inc()
            except Exception:
                pass

            if self._redis and self.redis_stream:
                try:
                    try:
                        await self._redis.xadd(
                            name=self.redis_stream,
                            fields={"tx": tx_hash, "hash": tx_hash, "ts_ms": str(int(ts_recv * 1000)), "ts": str(int(ts_recv))},
                            maxlen=self.redis_maxlen,
                            approximate=True,
                        )
                    except TypeError:
                        await self._redis.xadd(
                            self.redis_stream,
                            {"tx": tx_hash, "hash": tx_hash, "ts_ms": str(int(ts_recv * 1000)), "ts": str(int(ts_recv))}
                        )
                except Exception:
                    # do not crash the aggregator
                    pass

    def _extract_hashes(self, raw: Any) -> Iterable[str]:
        # tuple/list form from tests: ("msg", "0x...")
        if isinstance(raw, (tuple, list)) and len(raw) >= 2 and isinstance(raw[1], str):
            return [raw[1]] if raw[1].startswith("0x") else []

        # bytes -> str
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode()
            except Exception:
                return []

        # string or JSON-RPC string
        if isinstance(raw, str):
            s = raw.strip()
            if s.startswith("0x"):
                return [s]
            try:
                raw = json.loads(s)
            except Exception:
                return []

        # JSON-RPC dict
        if isinstance(raw, dict):
            params = raw.get("params") or {}
            res = params.get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return [res]
            if isinstance(res, dict):
                for k in ("hash", "transactionHash"):
                    v = res.get(k)
                    if isinstance(v, str) and v.startswith("0x"):
                        return [v]

        return []

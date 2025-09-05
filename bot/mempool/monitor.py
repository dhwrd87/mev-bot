# bot/mempool/monitor.py
import asyncio, json, os, random, time
from collections import deque
from typing import List, Optional, Set, Tuple
import logging 
from collections import deque

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from prometheus_client import start_http_server

from bot.core.telemetry import (
    mempool_rx_total, mempool_rx_errors_total, mempool_reconnects_total,
    mempool_ws_connected, mempool_unique_tx_total, mempool_tps, mempool_tpm,
    mempool_message_latency_ms,
)

SUBSCRIBE_MSG = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "eth_subscribe",
    "params": ["newPendingTransactions"]
}
# Some providers support "full" for tx bodies; we stick to hashes for speed
# "params": ["newPendingTransactions", {"includeTxArgs": False}]

class Backoff:
    def __init__(self, base=1.0, factor=2.0, max_delay=30.0, jitter=0.2):
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.n = 0

    def next(self) -> float:
        raw = min(self.base * (self.factor ** self.n), self.max_delay)
        self.n += 1
        # apply jitter ±jitter%
        j = raw * self.jitter
        return max(0.1, raw + random.uniform(-j, j))

    def reset(self):
        self.n = 0


class WSMempoolMonitor:
    """
    Multi-WS mempool racer:
      - Maintains N websocket subscriptions to Polygon RPC providers
      - Streams pending tx hashes into a shared queue
      - Dedups across endpoints
      - Tracks TPM/TPS on a rolling 60s window
      - Graceful reconnect with exponential backoff + jitter
    """
    def __init__(
        self,
        endpoints: List[str],
        metrics_port: Optional[int] = None,
        min_rate_target_tpm: int = 100,
        dedup_window_size: int = 100_000,
    ):
        self.endpoints = endpoints
        self.metrics_port = metrics_port
        self.min_rate_target_tpm = min_rate_target_tpm

        self._stop = asyncio.Event()
        self._queue: "asyncio.Queue[Tuple[str,float]]" = asyncio.Queue(maxsize=10_000)
        self._reader_tasks: List[asyncio.Task] = []
        self._agg_task: Optional[asyncio.Task] = None

        self._seen: Set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=dedup_window_size)
        self._timestamps: deque[float] = deque()  # timestamps of unique txs (for rolling rate)

        self._log = logging.getLogger("WSMempoolMonitor")

    async def start(self):
        if self.metrics_port:
            start_http_server(self.metrics_port)

        # Prime per-endpoint metrics so they appear immediately
        for ep in self.endpoints:
            mempool_ws_connected.labels(endpoint=ep).set(0)
            mempool_reconnects_total.labels(endpoint=ep).inc(0)
            mempool_rx_total.labels(endpoint=ep).inc(0)
            mempool_rx_errors_total.labels(endpoint=ep).inc(0)

        mempool_tps.set(0.0)
        mempool_tpm.set(0.0)

        for ep in self.endpoints:
            self._reader_tasks.append(asyncio.create_task(self._reader(ep)))
        self._agg_task = asyncio.create_task(self._aggregator())

    async def stop(self):
        self._stop.set()
        for t in self._reader_tasks:
            t.cancel()
        if self._agg_task:
            self._agg_task.cancel()

        # Drain tasks
        tasks = [*self._reader_tasks]
        if self._agg_task:
            tasks.append(self._agg_task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _reader(self, endpoint: str):
        """
        Persistent WS reader with reconnection + backoff.
        """
        backoff = Backoff()
        while not self._stop.is_set():
            ws = None
            try:
                ws = await websockets.connect(endpoint, ping_interval=20, ping_timeout=20)
                mempool_ws_connected.labels(endpoint=endpoint).set(1)
                backoff.reset()
                
                self._log.info("WS connected: %s", endpoint)

                # Subscribe
                await ws.send(json.dumps(SUBSCRIBE_MSG))

                # Listen loop
                while not self._stop.is_set():
                    raw = await ws.recv()
                    ts_recv = time.time()

                    # Parse notification; expect tx hash as "result"
                    try:
                        msg = json.loads(raw)
                        if "params" in msg and "result" in msg["params"]:
                            tx = msg["params"]["result"]
                        else:
                            # Sometimes providers send a heartbeat/result to the subscribe id
                            continue
                    except Exception:
                        # Non-JSON or unexpected shape
                        continue

                    mempool_rx_total.labels(endpoint=endpoint).inc()
                    mempool_message_latency_ms.observe(0.0)  # placeholder if you later add timing

                    # Push into queue (hash + timestamp)
                    try:
                        self._queue.put_nowait((tx, ts_recv))
                    except asyncio.QueueFull:
                        # Drop on overload (backpressure), but count error
                        mempool_rx_errors_total.labels(endpoint=endpoint).inc()

            except (ConnectionClosed, WebSocketException, OSError) as e:
                mempool_rx_errors_total.labels(endpoint=endpoint).inc()
                mempool_ws_connected.labels(endpoint=endpoint).set(0)
                mempool_reconnects_total.labels(endpoint=endpoint).inc()
                # Backoff
                delay = backoff.next()
                await asyncio.sleep(delay)
                self._log.warning("WS error (%s). Reconnecting in backoff… %s", endpoint, e)

            finally:
                try:
                    if ws:
                        await ws.close()
                        self._log.info("WS closed: %s", endpoint)
                except Exception:
                    pass

    async def _aggregator(self):
        """
        Dedup incoming hashes + maintain rolling 60s TPS/TPM.
        """
        while not self._stop.is_set():
            try:
                tx_hash, ts_recv = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                # Periodic rate update even if idle
                self._update_rates()
                continue

            if tx_hash in self._seen:
                # Already processed
                continue

            # Dedup insert (keep set and deque in lockstep to avoid leaks)
            if self._seen_order.maxlen and len(self._seen_order) >= self._seen_order.maxlen:
                oldest = self._seen_order.popleft()
                self._seen.discard(oldest)
            self._seen_order.append(tx_hash)
            self._seen.add(tx_hash)

            # Record timestamp for rate calc
            self._timestamps.append(ts_recv)
            mempool_unique_tx_total.inc()

            # Update rolling rates
            self._update_rates()

            # Warn if below target occasionally (non-spammy)
            # (You can add a timed gate if you want fewer logs.)

    def _update_rates(self):
        now = time.time()
        # Evict older than 60s
        while self._timestamps and now - self._timestamps[0] > 60.0:
            self._timestamps.popleft()

        count_60s = len(self._timestamps)
        tps = count_60s / 60.0
        mempool_tps.set(tps)
        mempool_tpm.set(count_60s)

        # Optional: you can emit a warning via alerts table if below target (handled elsewhere)

async def run_standalone():
    """
    Minimal runner for dev: reads Polygon WS endpoints from env and starts metrics on :8000
    """
    endpoints = [
        os.getenv("WS_POLYGON_1", "").strip(),
        os.getenv("WS_POLYGON_2", "").strip(),
        os.getenv("WS_POLYGON_3", "").strip(),
    ]
    endpoints = [e for e in endpoints if e]
    if not endpoints:
        raise SystemExit("No WS endpoints provided. Set WS_POLYGON_1/2/3.")

    monitor = WSMempoolMonitor(endpoints=endpoints, metrics_port=int(os.getenv("METRICS_PORT", "8000")))
    await monitor.start()
    try:
        # Run forever
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await monitor.stop()

if __name__ == "__main__":
    asyncio.run(run_standalone())

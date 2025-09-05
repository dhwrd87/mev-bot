import asyncio
import time
import pytest

from bot.mempool.monitor import WSMempoolMonitor

pytestmark = pytest.mark.asyncio

class FakeRedis:
    def __init__(self):
        self.added = 0
        self.last = None
    async def xadd(self, stream, fields):
        self.added += 1
        self.last = (stream, fields)

async def test_dedup_race_single_publish(monkeypatch):
    m = WSMempoolMonitor(endpoints=[], metrics_port=None, dedup_window_size=100, redis_url=None)
    m._redis = FakeRedis()  # inject fake publisher

    # Start aggregator
    task = asyncio.create_task(m._aggregator())

    # Enqueue the same tx twice (simulate two WS endpoints)
    now = time.time()
    await m._queue.put(("0xabc", now))
    await m._queue.put(("0xabc", now + 0.001))

    # Let aggregator process
    await asyncio.sleep(0.05)

    # Stop
    await m.stop()
    task.cancel()

    # Only one publish expected
    assert m._redis.added == 1
    # Rolling window should have exactly 1 entry
    assert len(m._timestamps) == 1

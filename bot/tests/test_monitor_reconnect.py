import asyncio
import json
import pytest
from types import SimpleNamespace
from websockets.exceptions import ConnectionClosed, WebSocketException

from bot.mempool.monitor import WSMempoolMonitor, SUBSCRIBE_MSG

pytestmark = pytest.mark.asyncio

class FakeWS:
    def __init__(self, messages):
        self._messages = messages
        self.closed = False
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._messages:
            await asyncio.sleep(0.01)
            raise ConnectionClosed(1006, "closed")
        return self._messages.pop(0)

    async def close(self):
        self.closed = True

async def test_reader_reconnect(monkeypatch):
    calls = {"n": 0}

    async def fake_connect(endpoint, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt: simulate failure
            raise WebSocketException("dial failed")
        # Second attempt: succeed and deliver one tx then close
        notif = {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {"subscription": "0x1", "result": "0xdeadbeef"}
        }
        return FakeWS(messages=[json.dumps(notif)])

    monkeypatch.setattr("websockets.connect", fake_connect)

    m = WSMempoolMonitor(endpoints=["wss://x"], metrics_port=None, dedup_window_size=100, redis_url=None)
    # Start only the reader task directly to keep the test fast
    task = asyncio.create_task(m._reader("wss://x"))

    # Let it run a moment, then stop
    await asyncio.sleep(0.1)
    await m.stop()
    await asyncio.sleep(0.05)
    task.cancel()

    # We expect websockets.connect to be called at least twice (fail -> reconnect -> success)
    assert calls["n"] >= 2

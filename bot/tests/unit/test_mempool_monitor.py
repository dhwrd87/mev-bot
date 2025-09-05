# tests/unit/test_mempool_monitor.py
import asyncio
import json
import types
import pytest
from unittest.mock import patch

from bot.mempool.monitor import WSMempoolMonitor, SUBSCRIBE_MSG

pytestmark = pytest.mark.asyncio

class FakeWS:
    """
    Minimal fake ws that yields scripted messages or raises once to simulate errors.
    """
    def __init__(self, script):
        # script: list of ('msg', value) or ('error', exc)
        self.script = script
        self.closed = False

    async def send(self, _):
        return

    async def recv(self):
        await asyncio.sleep(0)  # yield
        if not self.script:
            await asyncio.sleep(0.01)
            return json.dumps({"jsonrpc":"2.0","result":1,"id":1})  # no-op
        kind, value = self.script.pop(0)
        if kind == "msg":
            return json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {"subscription": "0x1", "result": value}
            })
        elif kind == "error":
            raise value

    async def close(self):
        self.closed = True


async def _drain_for(monitor: WSMempoolMonitor, seconds=0.2):
    # Let aggregator tick
    end = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < end:
        await asyncio.sleep(0.01)

@patch("websockets.connect")
async def test_race_dedup(connect_mock):
    """
    Two endpoints both emit same tx hash; ensure only one unique is counted.
    """
    h = "0xabc123"
    fake1 = FakeWS([("msg", h)])
    fake2 = FakeWS([("msg", h)])
    # Return a different fake per call
    connect_mock.side_effect = [fake1, fake2]

    m = WSMempoolMonitor(endpoints=["ws://a", "ws://b"], metrics_port=None)
    await m.start()
    await _drain_for(m, 0.2)
    # unique counter increments once; we can also check rolling window count via tpm > 0
    assert len(m._timestamps) == 1
    await m.stop()

@patch("websockets.connect")
async def test_reconnect_on_error(connect_mock):
    """
    WS raises once, then succeeds; monitor should reconnect and process messages.
    """
    from websockets.exceptions import ConnectionClosedError

    h = "0xdef456"
    # First connection raises error immediately on recv, second connection returns a message
    bad = FakeWS([("error", ConnectionClosedError(1006, "abnormal"))])
    good = FakeWS([("msg", h)])

    connect_mock.side_effect = [bad, good]

    m = WSMempoolMonitor(endpoints=["ws://unstable"], metrics_port=None)
    await m.start()
    await _drain_for(m, 0.4)
    assert len(m._timestamps) == 1  # eventually saw the message after reconnect
    await m.stop()

@patch("websockets.connect")
async def test_subscribe_sent(connect_mock):
    """
    Ensure we send the subscription payload on connect.
    """
    sent_payloads = []
    class SpyWS(FakeWS):
        async def send(self, payload):
            sent_payloads.append(payload)
            return
    spy = SpyWS([("msg", "0x1")])
    connect_mock.return_value = spy

    m = WSMempoolMonitor(endpoints=["ws://p"], metrics_port=None)
    await m.start()
    await _drain_for(m, 0.2)
    await m.stop()

    assert sent_payloads, "No subscribe call was sent"
    assert json.loads(sent_payloads[0]) == SUBSCRIBE_MSG

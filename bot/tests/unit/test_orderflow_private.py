import pytest, json
import respx, httpx
from bot.exec.orderflow import PrivateOrderflowManager, Endpoint, TxMeta

pytestmark = pytest.mark.asyncio

@respx.mock
async def test_fallback_then_success():
    bad = respx.post("https://bad.private").mock(return_value=httpx.Response(502, json={"error":"upstream"}))
    good = respx.post("https://good.private").mock(return_value=httpx.Response(200, json={"result":"ok"}))
    mgr = PrivateOrderflowManager([
        Endpoint("bad","rpc","https://bad.private"),
        Endpoint("good","rpc","https://good.private"),
    ], timeout_s=1, max_retries=1, base_backoff_s=0.01)
    out = await mgr.submit_private_bundle(["0xdeadbeef"], TxMeta(chain="polygon"))
    assert out["ok"] and out["endpoint"]=="good"
    assert bad.called and good.called

@respx.mock
async def test_all_fail_raises():
    respx.post("https://x").mock(return_value=httpx.Response(500, json={"error":"nope"}))
    mgr = PrivateOrderflowManager([Endpoint("x","flashbots","https://x")], timeout_s=1, max_retries=0)
    with pytest.raises(Exception) as e:
        await mgr.submit_private_bundle(["0xaaa"], TxMeta(chain="polygon"))
    assert "attempts" in str(e.value)

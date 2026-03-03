import pytest
from unittest.mock import patch
from bot.exec.bundle_builder import Bundle, RawTx, BundleSubmitter
pytestmark = pytest.mark.asyncio

def mk_bundle():
    return Bundle.new([RawTx("0xTARGET"), RawTx("0xOUR")], current_block=1000, skew=0)

@patch("bot.exec.bundle_builder.BuilderClient._post")
async def test_atomic_order_and_success(mock_post):
    def _sim_ok(payload, timeout):
        if payload["method"] == "eth_callBundle": return {"result": {"coinbaseDiff":"0x0"}}
        return {"result": "0xBUNDLE_TAG"}
    mock_post.side_effect = _sim_ok
    s = BundleSubmitter(chain="polygon")
    tag = await s.submit(mk_bundle())
    assert tag == "0xBUNDLE_TAG"

@patch("bot.exec.bundle_builder.BuilderClient._post")
async def test_retry_then_fallback(mock_post):
    calls = {"n": 0}
    def _flaky(payload, timeout):
        calls["n"] += 1
        if payload["method"] == "eth_callBundle": return {"result": {}}
        if calls["n"] == 2: return {"error": {"message": "timeout"}}
        if calls["n"] == 3: return {"error": {"message": "temporarily unavailable"}}
        return {"result": "0xOK_SECOND"}
    mock_post.side_effect = _flaky
    s = BundleSubmitter(chain="polygon")
    tag = await s.submit(mk_bundle())
    assert tag == "0xOK_SECOND"

@patch("bot.exec.bundle_builder.BuilderClient._post")
async def test_simulation_blocking(mock_post):
    def _sim_fail(payload, timeout):
        if payload["method"] == "eth_callBundle": return {"error": {"message": "revert: simulation failed"}}
        return {"error": {"message": "should_not_be_called"}}
    mock_post.side_effect = _sim_fail
    s = BundleSubmitter(chain="polygon")
    tag = await s.submit(mk_bundle())
    assert tag is None

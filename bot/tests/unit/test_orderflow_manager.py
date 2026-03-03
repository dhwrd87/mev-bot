# tests/unit/test_orderflow_manager.py
import pytest, json, asyncio
from unittest.mock import patch, AsyncMock
from bot.exec.orderflow import PrivateOrderflowManager, Endpoint, TxMeta, PrivateOrderflowError

pytestmark = pytest.mark.asyncio

@patch("httpx.AsyncClient.post")
async def test_success_first_endpoint(post_mock):
    post_mock.return_value = AsyncMock(status_code=200, json=lambda: {"result": "0xHASH"})
    mgr = PrivateOrderflowManager([Endpoint("p1","rpc","https://p1")])
    out = await mgr.submit_private_bundle(["0xdead"], TxMeta(chain="polygon"))
    assert out["ok"] and out["endpoint"] == "p1" and out["result"] == "0xHASH"

@patch("httpx.AsyncClient.post")
async def test_retry_then_public_fallback(post_mock):
    # two private failures, then public success
    post_mock.side_effect = [
        AsyncMock(status_code=500, json=lambda: {"error":"x"}),
        AsyncMock(status_code=500, json=lambda: {"error":"x"}),
        AsyncMock(status_code=200, json=lambda: {"result":"0xPUB"}),
    ]
    mgr = PrivateOrderflowManager([Endpoint("p1","rpc","https://p1")], max_retries=0)
    out = await mgr.submit_private_bundle(["0xdead"], TxMeta(chain="polygon", public_rpc_url="https://pub"))
    assert out["ok"] and out["endpoint"] == "public_fallback" and out["result"] == "0xPUB"

@patch("httpx.AsyncClient.post")
async def test_all_fail_raises(post_mock):
    post_mock.return_value = AsyncMock(status_code=500, json=lambda: {"error":"x"})
    mgr = PrivateOrderflowManager([Endpoint("p1","rpc","https://p1")], max_retries=1)
    with pytest.raises(PrivateOrderflowError):
        await mgr.submit_private_bundle(["0xdead"], TxMeta(chain="polygon"))

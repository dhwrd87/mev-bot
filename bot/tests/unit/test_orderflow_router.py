# tests/unit/test_orderflow_router.py
import asyncio, json, pytest
from unittest.mock import patch, AsyncMock
from bot.exec.orderflow import PrivateOrderflowRouter, Relay

pytestmark = pytest.mark.asyncio

def _router_with(relay_names=("flashbots_protect","mev_blocker"), chain="ethereum"):
    relays = [Relay(n, f"https://{n}.example", {}, chain) for n in relay_names]
    return PrivateOrderflowRouter({chain: relays}, {chain: "https://public.example"})

@patch("httpx.AsyncClient.post")
async def test_picks_private_then_public_on_fail(post_mock):
    # Fail relay, succeed on public
    post_mock.side_effect = [
        AsyncMock(status_code=200, json=lambda: {"error":{"code":-32000,"message":"fail"}})(),  # relay error
        AsyncMock(status_code=200, json=lambda: {"result":"0xHASH"})(),                         # public ok
    ]
    import os
    os.environ["BOT_RUNTIME_STATE"] = "TRADING"
    r = _router_with()
    res = await r.submit("0xsigned", "ethereum", {"high_slippage":True, "token_new":True, "detected_snipers":1, "value_usd":10_000})
    assert res["ok"] and res["route"] == "public"

@patch("httpx.AsyncClient.post")
async def test_public_when_no_flags(post_mock):
    post_mock.return_value = AsyncMock(status_code=200, json=lambda: {"result":"0xHASH"})()
    import os
    os.environ["BOT_RUNTIME_STATE"] = "TRADING"
    r = _router_with()
    res = await r.submit("0xsigned", "ethereum", {"high_slippage":False, "token_new":False, "detected_snipers":0, "value_usd":10})
    assert res["ok"] and res["route"] == "public"

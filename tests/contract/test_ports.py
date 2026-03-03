import os
import pytest

from bot.ports.fakes import (
    FakeRpcClient,
    FakePrivateOrderflowClient,
    FakeReceiptProvider,
    FakeOpportunityRepo,
    FakeTradeRepo,
    FakeRiskRepo,
    FakeAlertRepo,
)

pytestmark = pytest.mark.asyncio


async def test_fake_rpc_client_contract():
    c = FakeRpcClient()
    assert await c.get_tx("0xdead") is None
    assert isinstance(await c.gas_price(), int)
    assert await c.latest_block() is not None
    assert isinstance(await c.nonce("0xabc"), int)


async def test_fake_private_orderflow_contract():
    c = FakePrivateOrderflowClient()
    res = await c.submit_tx("0xraw", chain="sepolia")
    assert res.ok is True
    assert res.tx_hash
    assert res.relay


async def test_fake_receipt_provider_contract():
    c = FakeReceiptProvider()
    r = await c.wait_for_receipt("0xhash")
    assert r and r.get("status") == 1


async def test_fake_repos_contract():
    o = FakeOpportunityRepo()
    t = FakeTradeRepo()
    r = FakeRiskRepo()
    a = FakeAlertRepo()

    opp_id = await o.insert_opportunity({"chain": "sepolia"})
    trade_id = await t.insert_trade({"mode": "stealth"})
    await t.update_trade_outcome(id=trade_id, status="included")
    await r.record_state({"daily_pnl": 1.0})
    await a.send_alert("info", "ok", {"k": "v"})

    assert opp_id == 1
    assert trade_id == 1
    assert t.records[0]["status"] == "included"
    assert r.records
    assert a.records


@pytest.mark.skipif(os.getenv("REAL_PORTS") != "1", reason="REAL_PORTS not enabled")
async def test_real_ports_imports():
    from bot.ports.real import (
        RealRpcClient,
        RealPrivateOrderflowClient,
        RealReceiptProvider,
        RealTradeRepo,
        RealOpportunityRepo,
        RealRiskRepo,
        RealAlertRepo,
    )

    _ = RealRpcClient()
    _ = RealPrivateOrderflowClient(chain=os.getenv("CHAIN", "sepolia"))
    _ = RealReceiptProvider(http_url=os.getenv("RPC_ENDPOINT_PRIMARY", ""))
    _ = RealTradeRepo()
    _ = RealOpportunityRepo()
    _ = RealRiskRepo()
    _ = RealAlertRepo()

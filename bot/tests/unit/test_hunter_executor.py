import pytest, time
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.hunter.executor import BackrunExecutor
from bot.exec.orderflow import PrivateOrderflowManager, Endpoint
from bot.exec.permit2 import Permit2Handler, InMemoryNonceStore

pytestmark = pytest.mark.asyncio

class DummyOF(PrivateOrderflowManager):
    def __init__(self): pass
    async def submit_private_bundle(self, signed_txs_hex, meta, **_):
        return {"ok": True, "bundle": True}

async def test_build_and_submit(monkeypatch):
    w3 = Web3(EthereumTesterProvider())
    of = DummyOF()
    p2 = Permit2Handler(w3, InMemoryNonceStore())
    ex = BackrunExecutor(w3, of, "0x0000000000000000000000000000000000009999", p2)

    class Signed:  # mimic web3 Account.sign_transaction return
        def __init__(self): self.rawTransaction = b"\x01"
    def signer(tx): return Signed()

    class Opp: pass
    opp = Opp()
    opp.token_in = "0x0000000000000000000000000000000000000001"
    opp.token_out = "0x0000000000000000000000000000000000000002"
    opp.fee_tier = 3000

    def sizing(_): return (10**6, 2*10**6, 3000)
    plan = await ex.execute(
        owner=w3.eth.accounts[0],
        owner_priv="0x"+"11"*32,
        opp=opp, sizing_func=sizing,
        deadline_ts=int(time.time())+120,
        sign_account=signer
    )
    assert plan.ok and plan.info["bundle"] is True

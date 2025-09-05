import pytest, time
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.hunter.executor import BackrunExecutor
from bot.exec.orderflow import PrivateOrderflowManager, TxMeta
from bot.exec.permit2 import Permit2Handler, InMemoryNonceStore

pytestmark = pytest.mark.asyncio

class DummyOF(PrivateOrderflowManager):
    def __init__(self): pass
    async def submit_private_bundle(self, signed_txs_hex, meta, **_):
        return {"ok": True, "bundle": True, "n": len(signed_txs_hex)}

class Signed:
    def __init__(self): self.rawTransaction = b"\x01"

def signer(tx): return Signed()

async def test_executor_v3_and_v2_paths():
    w3 = Web3(EthereumTesterProvider())
    of = DummyOF()
    p2 = Permit2Handler(w3, InMemoryNonceStore())
    ex = BackrunExecutor(
        w3, of, v3_router="0x0000000000000000000000000000000000007777",
        v2_router="0x0000000000000000000000000000000000008888", permit2=p2
    )

    # V3 path
    plan1 = await ex.execute(
        owner=w3.eth.accounts[0], owner_priv="0x"+"11"*32,
        route_kind="v3", fee_or_bps=3000,
        token_in="0x0000000000000000000000000000000000000001", token_out="0x0000000000000000000000000000000000000002",
        want_out=10**6, max_in=2*10**6, deadline_ts=int(time.time())+120,
        sign_account=signer
    )
    assert plan1.ok and plan1.info["bundle"] is True

    # V2 path (will include approve+swap; allow allowance=0 by default)
    plan2 = await ex.execute(
        owner=w3.eth.accounts[0], owner_priv="0x"+"11"*32,
        route_kind="v2", fee_or_bps=30,
        token_in="0x0000000000000000000000000000000000000001", token_out="0x0000000000000000000000000000000000000002",
        want_out=10**6, max_in=2*10**6, deadline_ts=int(time.time())+120,
        sign_account=signer
    )
    assert plan2.ok and plan2.info["bundle"] is True and plan2.info["n"] >= 1

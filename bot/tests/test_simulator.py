import json, time, pytest
from unittest.mock import AsyncMock, patch
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from web3.exceptions import ContractLogicError

from bot.exec.simulator import PreSubmitSimulator, SwapSimResult
from bot.exec.exact_output import ExactOutputParams
from bot.exec.orderflow import Endpoint

pytestmark = pytest.mark.asyncio

def _w3():
    w3 = Web3(EthereumTesterProvider())
    w3.eth.chain_id = 137
    return w3

def _params(router="0x0000000000000000000000000000000000009999"):
    now = int(time.time())
    return ExactOutputParams(
        router=router,
        token_in="0x0000000000000000000000000000000000000001",
        token_out="0x0000000000000000000000000000000000000002",
        fee=3000,
        recipient="0x0000000000000000000000000000000000000003",
        deadline=now+600,
        amount_out_exact=1000,
        amount_in_max=2000,
        sqrt_price_limit_x96=0
    )

@patch("bot.exec.simulator.ExactOutputSwapper")
async def test_simulate_swap_success(_, monkeypatch):
    w3 = _w3()
    # Spy: contract call returns amountIn less than max
    class FakeFn:
        def call(self, _): return 1500
    class FakeContract:
        def __init__(self): pass
        class functions:
            @staticmethod
            def exactOutputSingle(_): return FakeFn()
    monkeypatch.setattr(w3.eth, "contract", lambda address, abi: FakeContract())

    sim = PreSubmitSimulator(w3, [])
    res = await sim.simulate_swap(_params(), sender=w3.eth.accounts[0])
    assert res.ok and res.amount_in == 1500

@patch("bot.exec.simulator.ExactOutputSwapper")
async def test_simulate_swap_policy_block(_, monkeypatch):
    w3 = _w3()
    class FakeFn:  # returns amountIn higher than max ⇒ policy fail
        def call(self, _): return 2500
    class FakeContract:
        class functions:
            @staticmethod
            def exactOutputSingle(_): return FakeFn()
    monkeypatch.setattr(w3.eth, "contract", lambda address, abi: FakeContract())

    sim = PreSubmitSimulator(w3, [])
    res = await sim.simulate_swap(_params(), sender=w3.eth.accounts[0])
    assert not res.ok and res.reason == "amount_in_exceeds_max"

@patch("bot.exec.simulator.ExactOutputSwapper")
async def test_simulate_swap_revert(_, monkeypatch):
    w3 = _w3()
    class FakeFn:
        def call(self, _): raise ContractLogicError("ERC20: insufficient allowance")
    class FakeContract:
        class functions:
            @staticmethod
            def exactOutputSingle(_): return FakeFn()
    monkeypatch.setattr(w3.eth, "contract", lambda address, abi: FakeContract())

    sim = PreSubmitSimulator(w3, [])
    res = await sim.simulate_swap(_params(), sender=w3.eth.accounts[0])
    assert not res.ok and "insufficient allowance" in (res.reason or "").lower()

@patch("httpx.AsyncClient.post")
async def test_simulate_bundle_success(post_mock):
    # Builder returns results with no reverts
    post_mock.return_value = type("R", (), {
        "status_code": 200,
        "json": lambda: {"jsonrpc":"2.0","result":{"bundleHash":"0xabc","results":[{"gasUsed":"0x1"}]}}
    })()
    w3 = _w3()
    sim = PreSubmitSimulator(w3, [Endpoint(name="builder", url="https://x", kind="builder", method_send_bundle="eth_callBundle")])
    res = await sim.simulate_bundle(["0x01","0x02"])
    assert res.ok and res.endpoint == "builder"

@patch("httpx.AsyncClient.post")
async def test_simulate_bundle_revert(post_mock):
    post_mock.return_value = type("R", (), {
        "status_code": 200,
        "json": lambda: {"jsonrpc":"2.0","result":{"results":[{"error":"revert reason"}]}}
    })()
    w3 = _w3()
    sim = PreSubmitSimulator(w3, [Endpoint(name="builder", url="https://x", kind="builder", method_send_bundle="eth_callBundle")])
    res = await sim.simulate_bundle(["0x01"])
    assert not res.ok and res.endpoint == "builder"

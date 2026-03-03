from __future__ import annotations
import asyncio
import pytest
from dataclasses import dataclass

from bot.strategy.stealth import StealthStrategy
from bot.exec.orderflow import PrivateOrderflowRouter, SubmitResult

pytestmark = pytest.mark.asyncio

# --- Relay stubs for private path success ---

@dataclass
class StubSubmitResult(SubmitResult):
    gas_used: int = 120_000
    gas_price_gwei: float = 25.0

class OkRelay:
    def __init__(self, name, chain="polygon"): self.name=name; self.chain=chain
    async def submit_raw(self, tx_hex, metadata): return StubSubmitResult(True, "0xok", self.name, None)
    def is_retryable(self, err): return False
    def classify_reason(self, err): return "none"

class SlowThenOkRelay(OkRelay):
    def __init__(self, name, chain="polygon"): super().__init__(name, chain); self.calls=0
    async def submit_raw(self, tx_hex, metadata):
        self.calls += 1
        if self.calls == 1:
            # simulate one transient error to exercise retry
            return StubSubmitResult(False, None, self.name, "temporarily unavailable")
        return await super().submit_raw(tx_hex, metadata)
    def is_retryable(self, err): return "temporarily" in (err or "")

@pytest.fixture(autouse=True)
def patch_relays(monkeypatch):
    # Patch the concrete clients used by PrivateOrderflowRouter to use our stubs
    from bot.exec import orderflow as of
    monkeypatch.setattr(of, "FlashbotsClient", lambda chain, url: SlowThenOkRelay("flashbots_protect", chain))
    monkeypatch.setattr(of, "MevBlockerClient", lambda chain, url: OkRelay("mev_blocker", chain))
    monkeypatch.setattr(of, "CowClient", lambda chain, url: OkRelay("cow_protocol", chain))
    yield

@pytest.fixture
def stealth_strategy():
    s = StealthStrategy()
    return s

def _params(i: int):
    # Slightly vary trade sizes; keep it easy to satisfy gas < 0.5% of notional
    size = 8_000 + i * 300  # USD
    return {
        "chain": "polygon",
        "token_in": "USDC",
        "token_out": "TOKENX",
        "amount_in": 1_000_000,            # 1,000,000 USDC wei-like (mock)
        "desired_output": 100_000,         # mock
        "max_input": 1_200_000,            # mock
        "router": "0xRouterV3",
        "sender": "0xSender",
        "recipient": "0xRecipient",
        "pool_fee": 3000,
        "deadline": None,
        "nonce": None,
        "size_usd": float(size),
        "eth_usd": 2500.0,
        "detected_snipers": 1,             # keep traits pushing private
    }

async def _run_one(stealth_strategy, i):
    params = _params(i)
    result = await stealth_strategy.execute_stealth_swap(params)
    assert result.success, f"Trade {i} failed: {result.notes}"
    assert result.sandwiched is False
    # Gas ratio check
    ratio = float(result.notes.get("gas_cost_ratio", 0.0))
    assert ratio < 0.005, f"Gas ratio too high: {ratio:.4%}"
    assert result.notes.get("relay") in ("flashbots_protect", "mev_blocker", "cow_protocol")
    return result

async def test_stealth_e2e_10_trades(stealth_strategy):
    # Run 10 trades concurrently to simulate throughput
    results = await asyncio.gather(*[ _run_one(stealth_strategy, i) for i in range(10) ])
    # All succeeded via private path (our router only uses private clients)
    assert len(results) == 10

import os
from pytest import mark

@mark.skipif(not os.getenv("POLYGON_RPC"), reason="fork mode requires POLYGON_RPC and anvil running")
async def test_stealth_e2e_fork_skeleton(stealth_strategy):
    """
    Skeleton for real fork validation:
    - Start: anvil --fork-url $POLYGON_RPC --port 8545
    - TODO: wire real UniswapV3 router + exactOutput, use small dust swaps
    - Submit via MEV-Blocker/Protect; assert mined and compare pre/post pool reserves to verify no sandwich.
    """
    # Minimal assertion to ensure route path works; wire real clients when ready.
    res = await stealth_strategy.execute_stealth_swap(_params(0))
    assert res.success is True
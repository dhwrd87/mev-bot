import asyncio
import pytest
from unittest.mock import patch

from bot.hunter.pipeline import process_candidate, PoolReserves, SignerStub
from bot.mempool.detectors import TxFeatures

pytestmark = pytest.mark.asyncio

def make_features(i: int, positive=True) -> TxFeatures:
    # Craft snipery features; every third one less profitable to exercise rejections
    amt_usd = 8_000 + 400 * i
    base = dict(
        chain="polygon", pair_id="USDC-TOKENX", is_uniswap_like=True, path_len=2,
        token_age_hours=0.3, is_trending=True, slippage_tolerance=0.07,
        amount_in_usd=amt_usd, pool_liquidity_usd=1_000_000,
        base_fee_gwei=30.0, priority_fee_gwei=80.0  # high priority
    )
    if not positive:
        base.update(dict(amount_in_usd=600.0, pool_liquidity_usd=60_000))  # too small/liquidity low
    f = TxFeatures(**base)
    # Attach a synthetic target raw tx hex
    setattr(f, "raw_signed_tx_hex", f"0xTARGET_{i:02d}")
    return f

class _PostBehavior:
    """Stateful mock for BuilderClient._post: sim ok; send success for most."""
    def __init__(self):
        self.calls = 0
    def __call__(self, payload, timeout_ms):
        method = payload.get("method")
        self.calls += 1
        if method == "eth_callBundle":
            return {"result": {"simulated": True}}
        # eth_sendBundle
        # Fail every 4th send once to exercise fallback; then succeed
        if (self.calls % 4) == 0:
            return {"error": {"message": "temporarily unavailable"}}
        return {"result": "0xBUNDLE_OK"}

@patch("bot.exec.bundle_builder.BuilderClient._post")
async def test_hunter_e2e_8_candidates(mock_post):
    # Arrange mock behavior
    mock_post.side_effect = _PostBehavior()

    # Create 8 candidates (6 strong, 2 weak)
    candidates = [make_features(i, positive=(i % 4 != 3)) for i in range(8)]
    reserves = PoolReserves(r_in=100_000_000, r_out=50_000_000)
    signer = SignerStub()

    async def run_one(f):
        return await process_candidate(f, reserves, current_block=12_345_678, signer=signer)

    results = await asyncio.gather(*[run_one(f) for f in candidates])

    successes = [r for r in results if r["ok"]]
    total_profit = sum(r["expected_profit_usd"] for r in successes)

    # Acceptance checks
    assert len(successes) >= 5, f"Expected ≥5 successes, got {len(successes)}"
    assert total_profit > 0.0, f"Expected positive total expected P&L, got {total_profit}"
    # Ensure bundles target current block or greater
    assert all(r["target_block"] >= 12_345_678 for r in successes)

import os
from pytest import mark

@mark.skipif(not os.getenv("POLYGON_RPC"), reason="needs fork + real submit wiring")
async def test_hunter_e2e_fork_skeleton():
    """
    TODO:
      - Run anvil --fork-url $POLYGON_RPC
      - Use real decode -> reserves -> calc -> sign (small dust)
      - Submit bundle to a test builder; assert send returns non-empty result
    """
    assert True

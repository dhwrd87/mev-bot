import pytest
import math
from bot.hunter.backrun_calc import estimate_backrun, TargetSwap
from bot.hunter.adapter_stub import StaticPricingAdapter

pytestmark = pytest.mark.asyncio

def tswap(**kw):
    base = dict(
        chain="polygon", dex="v2", pool_fee_bps=30,
        token_in="USDC", token_out="TOKENX",
        amount_in=1_000_000,            # mock units
        amount_in_usd=10_000.0,
        pool_liquidity_usd=1_000_000.0,
        base_fee_gwei=30.0, priority_fee_gwei=20.0
    )
    base.update(kw); return TargetSwap(**base)

async def test_profitable_candidate_accepts():
    # reserves imply decent price impact capture
    reserves_in = 100_000_000
    reserves_out = 50_000_000
    opp = await estimate_backrun(tswap(), StaticPricingAdapter(eth_usd=2500), reserves_in, reserves_out)
    assert opp is not None, "should accept profitable opportunity"
    assert opp.expected_profit_usd > 0
    assert opp.context["gas_cost_usd"] / max(1, opp.context["gross_gain_usd"]) < 0.30

async def test_low_liquidity_rejects():
    opp = await estimate_backrun(tswap(pool_liquidity_usd=50_000), StaticPricingAdapter(), 10_000_000, 5_000_000)
    assert opp is None

async def test_below_min_profit_rejects(monkeypatch):
    # Increase gas dramatically to kill profit
    opp = await estimate_backrun(
        tswap(base_fee_gwei=150.0, priority_fee_gwei=100.0),
        StaticPricingAdapter(eth_usd=3000),
        20_000_000, 10_000_000
    )
    assert opp is None

async def test_gas_ratio_too_high_rejects(monkeypatch):
    # Tiny gross gain vs fixed gas -> ratio too high
    opp = await estimate_backrun(
        tswap(amount_in=50_000, amount_in_usd=500.0),
        StaticPricingAdapter(eth_usd=1800),
        1_000_000_000, 1_000_000_000
    )
    assert opp is None

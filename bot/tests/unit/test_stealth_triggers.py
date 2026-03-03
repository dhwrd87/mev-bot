import pytest
from bot.strategy.stealth_triggers import TradeContext, evaluate_stealth

def ctx(**kw):
    defaults = dict(
        estimated_slippage=0.002, token_age_hours=48, liquidity_usd=500000,
        is_trending=False, detected_snipers=0, size_usd=1000, gas_gwei=30
    )
    defaults.update(kw); return TradeContext(**defaults)

def test_no_flags_no_stealth(settings):  # if you expose settings fixture; else remove arg
    go, reasons = evaluate_stealth(ctx())
    assert go is False
    assert reasons == []

def test_high_slippage_trending_triggers_go():
    go, reasons = evaluate_stealth(ctx(estimated_slippage=0.02, is_trending=True))
    assert go is True
    assert "high_slippage" in reasons and "trending" in reasons

def test_new_token_low_liq_triggers_go():
    go, reasons = evaluate_stealth(ctx(token_age_hours=2, liquidity_usd=50_000))
    assert go is True
    assert set(reasons) >= {"new_token","low_liquidity"}

def test_large_trade_only_one_flag_not_enough(monkeypatch):
    # Ensure min_flags=2 behavior
    go, reasons = evaluate_stealth(ctx(size_usd=50_000))
    assert go is False
    assert "large_trade" in reasons and len(reasons) == 1

def test_gas_spike_and_snipers_triggers_go():
    go, reasons = evaluate_stealth(ctx(gas_gwei=150, detected_snipers=2))
    assert go is True
    assert set(reasons) >= {"gas_spike","active_snipers"}

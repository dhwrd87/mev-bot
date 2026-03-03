import pytest
from bot.mempool.detectors import TxFeatures, is_sniper, is_sandwich_victim, evaluate_on_fixtures
pytestmark = pytest.mark.asyncio

def f_sniper(**kw):
    base = dict(token_age_hours=0.3,is_trending=True,path_len=2,amount_in_usd=1500,slippage_tolerance=0.07,base_fee_gwei=40,priority_fee_gwei=100)
    base.update(kw); return TxFeatures(**base)

def f_not_sniper(**kw):
    base = dict(token_age_hours=48,is_trending=False,path_len=3,amount_in_usd=50,slippage_tolerance=0.005,base_fee_gwei=40,priority_fee_gwei=10)
    base.update(kw); return TxFeatures(**base)

def f_victim(**kw):
    base = dict(is_exact_output=False,path_len=2,amount_in_usd=40000,pool_liquidity_usd=1_000_000,slippage_tolerance=0.02,deadline_seconds=45,base_fee_gwei=40,priority_fee_gwei=30)
    base.update(kw); return TxFeatures(**base)

def f_not_victim(**kw):
    base = dict(is_exact_output=True,path_len=3,amount_in_usd=500,pool_liquidity_usd=5_000_000,slippage_tolerance=0.002,deadline_seconds=600,base_fee_gwei=40,priority_fee_gwei=120)
    base.update(kw); return TxFeatures(**base)

def test_sniper_positive_basic():
    pred, score, reasons = is_sniper(f_sniper())
    assert pred is True and score >= 0.60 and {"new_token","high_priority_ratio","high_slippage"} <= set(reasons)

def test_sniper_negative_basic():
    pred, score, _ = is_sniper(f_not_sniper())
    assert pred is False and score < 0.60

def test_victim_positive_basic():
    pred, score, reasons = is_sandwich_victim(f_victim())
    assert pred is True and score >= 0.60 and {"exact_input","high_slippage","large_vs_pool"} <= set(reasons)

def test_victim_negative_basic():
    pred, score, _ = is_sandwich_victim(f_not_victim())
    assert pred is False and score < 0.60

def test_evaluate_on_fixtures_logs_metrics():
    sniper_fixtures = [
        (f_sniper(), True),
        (f_sniper(priority_fee_gwei=90), True),
        (f_not_sniper(), False),
        (f_not_sniper(slippage_tolerance=0.02), False),
        (f_not_sniper(token_age_hours=1.0, priority_fee_gwei=20, base_fee_gwei=20), True),
    ]
    victim_fixtures = [
        (f_victim(), True),
        (f_victim(amount_in_usd=25_000, pool_liquidity_usd=900_000), True),
        (f_not_victim(), False),
        (f_not_victim(slippage_tolerance=0.012), False),
        (f_not_victim(is_exact_output=False, slippage_tolerance=0.03, amount_in_usd=30_000, pool_liquidity_usd=2_000_000, deadline_seconds=30), True),
    ]
    s = evaluate_on_fixtures("sniper", sniper_fixtures)
    v = evaluate_on_fixtures("sandwich_victim", victim_fixtures)
    assert s["precision"] >= 0.75 and s["recall"] >= 0.75 and 0.0 <= s["false_positive_rate"] <= 0.25
    assert v["precision"] >= 0.75 and v["recall"] >= 0.75 and 0.0 <= v["false_positive_rate"] <= 0.25

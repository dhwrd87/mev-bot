import pytest

from bot.risk.adaptive import AdaptiveRiskManager, RiskConfig

pytestmark = pytest.mark.asyncio


def test_risk_manager_contract():
    rm = AdaptiveRiskManager(RiskConfig(capital_usd=10_000, max_position_size_pct=5.0, max_daily_loss_usd=1000.0, max_consecutive_losses=3))

    ok, reason = rm.should_execute({"size_usd": 100})
    assert isinstance(ok, bool)
    assert isinstance(reason, str)
    assert ok is True

    rm.record_result(-10.0)
    rm.record_result(-10.0)
    rm.record_result(-10.0)

    ok2, reason2 = rm.should_execute({"size_usd": 100})
    assert ok2 is False
    assert reason2 in {"consecutive_losses", "daily_drawdown", "position_cap"}

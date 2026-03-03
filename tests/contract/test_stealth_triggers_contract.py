import types
import pytest

from bot.strategy import stealth_triggers as st

pytestmark = pytest.mark.asyncio


def _inject_settings():
    st.settings = types.SimpleNamespace(
        stealth_strategy=types.SimpleNamespace(
            triggers=types.SimpleNamespace(
                min_flags=2,
                flags=types.SimpleNamespace(
                    high_slippage=0.005,
                    new_token_age_hours=24,
                    low_liquidity_usd=100000,
                    trending=True,
                    active_snipers_min=1,
                    large_trade_usd=20000,
                    gas_spike_gwei=120,
                ),
            )
        )
    )


def test_evaluate_stealth_contract():
    _inject_settings()
    ctx = st.TradeContext(
        estimated_slippage=0.02,
        token_age_hours=6,
        liquidity_usd=80_000,
        is_trending=True,
        detected_snipers=1,
        size_usd=8_000,
        gas_gwei=40,
    )
    go, reasons = st.evaluate_stealth(ctx)
    assert isinstance(go, bool)
    assert isinstance(reasons, list)
    assert go is True
    assert "high_slippage" in reasons

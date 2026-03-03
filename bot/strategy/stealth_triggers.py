# bot/strategy/stealth_triggers.py
from dataclasses import dataclass
from typing import Dict, List, Tuple
from bot.core.telemetry import (
    stealth_trigger_flags_total, stealth_decisions_total, stealth_flags_count
)
from bot.core.config import settings

@dataclass
class TradeContext:
    estimated_slippage: float
    token_age_hours: float
    liquidity_usd: float
    is_trending: bool
    detected_snipers: int
    size_usd: float
    gas_gwei: float

def _flag(name: str):
    stealth_trigger_flags_total.labels(flag=name).inc()

def evaluate_stealth(ctx: TradeContext) -> Tuple[bool, List[str]]:
    cfg = settings.stealth_strategy.triggers
    flags_cfg = cfg.flags

    fired: List[str] = []

    if ctx.estimated_slippage > float(flags_cfg.high_slippage):
        fired.append("high_slippage"); _flag("high_slippage")
    if ctx.token_age_hours < float(flags_cfg.new_token_age_hours):
        fired.append("new_token"); _flag("new_token")
    if ctx.liquidity_usd < float(flags_cfg.low_liquidity_usd):
        fired.append("low_liquidity"); _flag("low_liquidity")
    if bool(flags_cfg.trending) and ctx.is_trending:
        fired.append("trending"); _flag("trending")
    if ctx.detected_snipers >= int(flags_cfg.active_snipers_min):
        fired.append("active_snipers"); _flag("active_snipers")
    if ctx.size_usd >= float(flags_cfg.large_trade_usd):
        fired.append("large_trade"); _flag("large_trade")
    if ctx.gas_gwei >= float(flags_cfg.gas_spike_gwei):
        fired.append("gas_spike"); _flag("gas_spike")

    stealth_flags_count.set(len(fired))
    go = len(fired) >= int(cfg.min_flags)
    stealth_decisions_total.labels(decision="go" if go else "no_go").inc()
    return go, fired

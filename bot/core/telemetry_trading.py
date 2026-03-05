"""Trading-specific Prometheus metrics for opportunity, decision, risk, and PnL flows.

This module centralizes trading telemetry so workers/orchestrators/strategies can
record consistent labeled metrics across the stack.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram


def _collector_name(name: str) -> str:
    return f"{name}_total" if not name.endswith("_total") else name


def _get_or_create_counter(name: str, documentation: str, labelnames: list[str]) -> Counter:
    try:
        return Counter(name, documentation, labelnames)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(_collector_name(name))  # type: ignore[attr-defined]
        if isinstance(existing, Counter):
            return existing
        raise


def _get_or_create_histogram(
    name: str,
    documentation: str,
    labelnames: list[str],
    buckets: list[float],
) -> Histogram:
    try:
        return Histogram(name, documentation, labelnames, buckets=tuple(buckets))
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if isinstance(existing, Histogram):
            return existing
        raise


def _get_or_create_gauge(name: str, documentation: str, labelnames: list[str]) -> Gauge:
    try:
        return Gauge(name, documentation, labelnames)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if isinstance(existing, Gauge):
            return existing
        raise


# Opportunity metrics.
opportunities_detected_total = _get_or_create_counter(
    "opportunities_detected_total",
    "Total opportunities detected by detector and opportunity type.",
    ["family", "chain", "type", "detector"],
)
opportunities_scored = _get_or_create_histogram(
    "opportunities_scored",
    "Distribution of detector/opportunity scoring values.",
    ["family", "chain", "type"],
    [0, 1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

# Strategy decision metrics.
strategy_decisions_total = _get_or_create_counter(
    "strategy_decisions_total",
    "Total strategy decisions made by orchestrator, by mode/strategy/reason.",
    ["family", "chain", "mode", "strategy", "reason"],
)
strategy_decision_latency_ms = _get_or_create_histogram(
    "strategy_decision_latency_ms",
    "Latency for strategy selection decisions in milliseconds.",
    ["family", "chain", "mode"],
    [1, 5, 10, 25, 50, 100, 250, 500],
)

# Risk manager metrics.
risk_approvals_total = _get_or_create_counter(
    "risk_approvals_total",
    "Risk manager approvals by mode/outcome.",
    ["family", "chain", "mode", "outcome"],
)
risk_rejections_total = _get_or_create_counter(
    "risk_rejections_total",
    "Risk manager rejections by mode/reason.",
    ["family", "chain", "mode", "reason"],
)
risk_position_sizing = _get_or_create_histogram(
    "risk_position_sizing",
    "Approved position sizing in USD.",
    ["family", "chain", "mode", "strategy"],
    [10, 50, 100, 500, 1000, 5000, 10000],
)
risk_current_exposure_usd = _get_or_create_gauge(
    "risk_current_exposure_usd",
    "Current portfolio exposure in USD.",
    ["family", "chain"],
)
risk_daily_pnl_usd = _get_or_create_gauge(
    "risk_daily_pnl_usd",
    "Current daily PnL in USD from risk manager perspective.",
    ["family", "chain", "date"],
)
risk_consecutive_losses = _get_or_create_gauge(
    "risk_consecutive_losses",
    "Current consecutive losing-trade count.",
    ["family", "chain"],
)

# Execution metrics.
executions_attempted_total = _get_or_create_counter(
    "executions_attempted_total",
    "Total trade execution attempts.",
    ["family", "chain", "mode", "strategy"],
)
executions_completed_total = _get_or_create_counter(
    "executions_completed_total",
    "Total completed trade executions by final outcome.",
    ["family", "chain", "mode", "strategy", "outcome"],
)
execution_latency_ms = _get_or_create_histogram(
    "execution_latency_ms",
    "Execution end-to-end latency in milliseconds.",
    ["family", "chain", "mode", "strategy"],
    [50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

# Stealth mode metrics.
stealth_submissions_total = _get_or_create_counter(
    "stealth_submissions_total",
    "Stealth submission attempts by relay and outcome.",
    ["family", "chain", "relay", "outcome"],
)
stealth_relay_latency_ms = _get_or_create_histogram(
    "stealth_relay_latency_ms",
    "Stealth relay submission latency in milliseconds.",
    ["family", "chain", "relay"],
    [100, 250, 500, 1000, 2500, 5000],
)
stealth_permit2_success = _get_or_create_counter(
    "stealth_permit2_success",
    "Total successful stealth flows using Permit2.",
    ["family", "chain"],
)
stealth_exact_output_swaps = _get_or_create_counter(
    "stealth_exact_output_swaps",
    "Total stealth exact-output swaps by DEX.",
    ["family", "chain", "dex"],
)

# Hunter mode metrics.
hunter_bundles_submitted_total = _get_or_create_counter(
    "hunter_bundles_submitted_total",
    "Total hunter bundles submitted by builder and outcome.",
    ["family", "chain", "builder", "outcome"],
)
hunter_bundle_inclusion_rate = _get_or_create_gauge(
    "hunter_bundle_inclusion_rate",
    "Hunter bundle inclusion rate percentage per builder.",
    ["family", "chain", "builder"],
)
hunter_backrun_opportunities = _get_or_create_counter(
    "hunter_backrun_opportunities",
    "Total backrun opportunities identified by target type.",
    ["family", "chain", "target_type"],
)
hunter_sandwich_attempts = _get_or_create_counter(
    "hunter_sandwich_attempts",
    "Total sandwich attempts by outcome.",
    ["family", "chain", "outcome"],
)

# P&L metrics.
trade_expected_profit_usd = _get_or_create_histogram(
    "trade_expected_profit_usd",
    "Distribution of expected profit (USD) for executed opportunities.",
    ["family", "chain", "mode", "strategy"],
    [0.1, 0.5, 1, 5, 10, 25, 50, 100, 250, 500],
)
trade_realized_profit_usd = _get_or_create_histogram(
    "trade_realized_profit_usd",
    "Distribution of realized trade profit (USD), can include losses.",
    ["family", "chain", "mode", "strategy"],
    [-100, -50, -10, -1, 0, 1, 10, 50, 100, 250, 500],
)
trade_gas_cost_usd = _get_or_create_histogram(
    "trade_gas_cost_usd",
    "Distribution of gas costs in USD for executed trades.",
    ["family", "chain", "mode"],
    [0.5, 1, 2, 5, 10, 25, 50, 100],
)
trade_slippage_bps = _get_or_create_histogram(
    "trade_slippage_bps",
    "Observed slippage in basis points.",
    ["family", "chain", "mode", "dex"],
    [1, 5, 10, 25, 50, 100, 250, 500],
)
cumulative_pnl_usd = _get_or_create_gauge(
    "cumulative_pnl_usd",
    "Cumulative PnL in USD by mode.",
    ["family", "chain", "mode"],
)
daily_pnl_usd = _get_or_create_gauge(
    "daily_pnl_usd",
    "Daily PnL in USD by date label.",
    ["family", "chain", "date"],
)
trade_win_rate_pct = _get_or_create_gauge(
    "trade_win_rate_pct",
    "Trade win rate percentage by mode/strategy/window.",
    ["family", "chain", "mode", "strategy", "window"],
)

# Performance metrics.
strategy_performance_score = _get_or_create_gauge(
    "strategy_performance_score",
    "Composite strategy performance score in range 0..100.",
    ["family", "chain", "strategy", "window"],
)
avg_profit_per_trade = _get_or_create_gauge(
    "avg_profit_per_trade",
    "Average realized net profit per trade in USD.",
    ["family", "chain", "mode", "strategy", "window"],
)
profit_factor = _get_or_create_gauge(
    "profit_factor",
    "Profit factor (gross profit / gross loss).",
    ["family", "chain", "mode", "window"],
)

# System health metrics.
opportunity_processing_lag_ms = _get_or_create_gauge(
    "opportunity_processing_lag_ms",
    "Current lag between opportunity detection and processing in milliseconds.",
    ["family", "chain"],
)
strategy_active_count = _get_or_create_gauge(
    "strategy_active_count",
    "Current number of active strategies per mode.",
    ["family", "chain", "mode"],
)
pending_opportunities = _get_or_create_gauge(
    "pending_opportunities",
    "Current count of pending opportunities awaiting processing.",
    ["family", "chain"],
)


def record_opportunity_detected(family: str, chain: str, opp_type: str, detector: str, score: float) -> None:
    """Record a new opportunity and its score at detector output time."""
    opportunities_detected_total.labels(family, chain, opp_type, detector).inc()
    opportunities_scored.labels(family, chain, opp_type).observe(float(score))


def record_strategy_decision(
    family: str,
    chain: str,
    mode: str,
    strategy: str,
    reason: str,
    latency_ms: float,
) -> None:
    """Record orchestrator strategy-selection decision and latency."""
    strategy_decisions_total.labels(family, chain, mode, strategy, reason).inc()
    strategy_decision_latency_ms.labels(family, chain, mode).observe(float(latency_ms))


def record_risk_decision(
    family: str,
    chain: str,
    mode: str,
    approved: bool,
    reason: str,
    size_usd: float,
    strategy: str,
) -> None:
    """Record risk gate decision and approved size distribution when applicable."""
    if approved:
        risk_approvals_total.labels(family, chain, mode, "approved").inc()
        risk_position_sizing.labels(family, chain, mode, strategy).observe(float(size_usd))
    else:
        risk_rejections_total.labels(family, chain, mode, reason).inc()


def record_execution_result(
    family: str,
    chain: str,
    mode: str,
    strategy: str,
    success: bool,
    expected_profit: float,
    realized_profit: float,
    gas_cost: float,
    slippage_bps: float,
    latency_ms: float,
    dex: str = "unknown",
) -> None:
    """Record execution completion and, on success, trade economics distributions."""
    outcome = "success" if success else "failed"
    executions_completed_total.labels(family, chain, mode, strategy, outcome).inc()
    execution_latency_ms.labels(family, chain, mode, strategy).observe(float(latency_ms))

    if success:
        trade_expected_profit_usd.labels(family, chain, mode, strategy).observe(float(expected_profit))
        trade_realized_profit_usd.labels(family, chain, mode, strategy).observe(float(realized_profit))
        trade_gas_cost_usd.labels(family, chain, mode).observe(float(gas_cost))
        trade_slippage_bps.labels(family, chain, mode, dex).observe(float(slippage_bps))


def update_cumulative_pnl(family: str, chain: str, mode: str, pnl: float) -> None:
    """Set cumulative PnL gauge value for the given chain/mode."""
    cumulative_pnl_usd.labels(family, chain, mode).set(float(pnl))


def update_win_rate(family: str, chain: str, mode: str, strategy: str, window: str, rate: float) -> None:
    """Set trade win-rate percentage gauge for a given window."""
    trade_win_rate_pct.labels(family, chain, mode, strategy, window).set(float(rate))


def _safe_set(g: Gauge, labels: tuple[Any, ...], value: float) -> None:
    """Internal helper for optional integration points that set gauges safely."""
    g.labels(*labels).set(float(value))


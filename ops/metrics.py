from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from bot.core.canonical import ctx_labels

log = logging.getLogger("ops.metrics")

_SERVER_STARTED = False


def _norm(v: str | None, default: str = "unknown") -> str:
    s = str(v or "").strip().lower()
    return s or default


def _canon_ctx(
    *,
    family: str | None,
    chain: str | None,
    network: str | None = None,
    strategy: str | None = None,
    dex: str | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    c = ctx_labels(
        family=family,
        chain=chain,
        network=network,
        strategy=strategy,
        dex=dex,
        provider=provider,
    )
    c.setdefault("dex", _norm(dex))
    c.setdefault("strategy", _norm(strategy, default="default"))
    c.setdefault("provider", _norm(provider))
    return c


tx_sent_total = Counter(
    "mevbot_tx_sent_total",
    "Total transactions submitted for broadcast",
    ["family", "chain", "network", "strategy"],
)
tx_confirmed_total = Counter(
    "mevbot_tx_confirmed_total",
    "Total transactions confirmed on-chain",
    ["family", "chain", "network", "strategy"],
)
tx_failed_total = Counter(
    "mevbot_tx_failed_total",
    "Total transactions failed before confirmation",
    ["family", "chain", "network", "strategy", "reason"],
)
tx_confirm_latency_seconds = Histogram(
    "mevbot_tx_confirm_latency_seconds",
    "Transaction confirmation latency in seconds",
    ["family", "chain", "network", "strategy"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
)
rpc_latency_seconds = Histogram(
    "mevbot_rpc_latency_seconds",
    "RPC request latency in seconds",
    ["family", "chain", "network", "provider", "method"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)
rpc_errors_total = Counter(
    "mevbot_rpc_errors_total",
    "RPC errors by provider and normalized code bucket",
    ["family", "chain", "network", "provider", "code_bucket"],
)

bot_state_value = Gauge(
    "mevbot_bot_state_value",
    "DEPRECATED: use mevbot_runtime_state_value{family,chain,network} enum gauge instead",
    ["family", "chain", "network"],
)
runtime_state_value = Gauge(
    "mevbot_runtime_state_value",
    "Effective runtime bot state enum (UNKNOWN=0, PAUSED=1, READY=2, TRADING=3, DEGRADED=4, PANIC=5)",
    ["family", "chain", "network"],
)
effective_state_value = Gauge(
    "mevbot_effective_state_value",
    "Effective runtime bot state enum (UNKNOWN=0, PAUSED=1, READY=2, TRADING=3, DEGRADED=4, PANIC=5)",
    ["family", "chain", "network"],
)
desired_state_value = Gauge(
    "mevbot_desired_state_value",
    "Desired operator bot state enum (UNKNOWN=0, PAUSED=1, READY=2, TRADING=3, DEGRADED=4, PANIC=5)",
    ["family", "chain", "network"],
)
effective_chain_info = Gauge(
    "mevbot_effective_chain_info",
    "Effective runtime chain marker (always 1)",
    ["family", "chain", "network"],
)
desired_chain_info = Gauge(
    "mevbot_desired_chain_info",
    "Desired operator chain marker (always 1)",
    ["family", "chain", "network"],
)
effective_mode_info = Gauge(
    "mevbot_effective_mode_info",
    "Effective runtime mode enum (UNKNOWN=0, DRYRUN=1, PAPER=2, LIVE=3)",
    ["family", "chain", "network"],
)
desired_mode_info = Gauge(
    "mevbot_desired_mode_info",
    "Desired operator mode enum (UNKNOWN=0, DRYRUN=1, PAPER=2, LIVE=3)",
    ["family", "chain", "network"],
)
state_gauge = Gauge(
    "mevbot_state",
    "DEPRECATED: use mevbot_runtime_state_value; runtime state enum (UNKNOWN=0, PAUSED=1, READY=2, TRADING=3, DEGRADED=4, PANIC=5)",
    ["family", "chain", "network"],
)
runtime_context_info = Gauge(
    "mevbot_runtime_context_info",
    "Effective runtime context marker (always 1)",
    ["family", "chain", "network", "state", "mode"],
)
desired_context_info = Gauge(
    "mevbot_desired_context_info",
    "Desired operator context marker (always 1)",
    ["family", "chain", "network", "state", "mode"],
)
head_lag_blocks = Gauge(
    "mevbot_head_lag_blocks",
    "Head lag in blocks",
    ["family", "chain", "network", "provider"],
)
slot_lag = Gauge(
    "mevbot_slot_lag",
    "Slot lag on slot-based chains",
    ["family", "chain", "network", "provider"],
)
heartbeat_ts = Gauge(
    "mevbot_heartbeat_ts",
    "Bot heartbeat UNIX timestamp (seconds)",
    ["family", "chain", "network", "provider", "dex", "strategy"],
)
chain_info = Gauge(
    "mevbot_chain_info",
    "Canonical chain identity marker (value is always 1)",
    ["family", "chain", "network"],
)
chain_head = Gauge(
    "mevbot_chain_head",
    "Latest observed chain head height",
    ["family", "chain", "network", "provider"],
)
chain_slot = Gauge(
    "mevbot_chain_slot",
    "Latest observed chain slot",
    ["family", "chain", "network", "provider"],
)
pnl_realized_usd = Gauge(
    "mevbot_pnl_realized_usd",
    "Realized PnL in USD",
    ["family", "chain", "network", "strategy"],
)
fees_total_usd = Gauge(
    "mevbot_fees_total_usd",
    "Total fees paid in USD (gauge snapshot, monotonic by convention)",
    ["family", "chain", "network", "strategy"],
)
drawdown_usd = Gauge(
    "mevbot_drawdown_usd",
    "Current drawdown in USD",
    ["family", "chain", "network", "strategy"],
)

opportunities_seen_total = Counter(
    "mevbot_opportunities_seen_total",
    "Opportunities seen by detector",
    ["family", "chain", "network", "dex", "strategy"],
)
opportunities_attempted_total = Counter(
    "mevbot_opportunities_attempted_total",
    "Opportunities attempted for execution",
    ["family", "chain", "network", "dex", "strategy"],
)
opportunities_filled_total = Counter(
    "mevbot_opportunities_filled_total",
    "Opportunities successfully filled",
    ["family", "chain", "network", "dex", "strategy"],
)
opportunities_rejected_total = Counter(
    "mevbot_opportunities_rejected_total",
    "Opportunities rejected by orchestrator risk/sizing/sim gates",
    ["family", "chain", "network", "strategy", "reason"],
)
opportunities_simulated_total = Counter(
    "mevbot_opportunities_simulated_total",
    "Opportunities simulated by orchestrator before execution",
    ["family", "chain", "network", "strategy", "dex", "outcome"],
)
opportunities_executed_total = Counter(
    "mevbot_opportunities_executed_total",
    "Opportunities that produced executable plans or were executed",
    ["family", "chain", "network", "strategy", "dex", "mode"],
)
opportunity_queue_depth = Gauge(
    "mevbot_opportunity_queue_depth",
    "Current opportunity queue depth",
    ["family", "chain", "network", "strategy"],
)
tx_sent_by_dex_type_total = Counter(
    "mevbot_tx_sent_by_dex_type_total",
    "Transactions sent by dex and opportunity type",
    ["family", "chain", "network", "dex", "type"],
)
dex_quote_total = Counter(
    "mevbot_dex_quote_total",
    "DEX quote attempts",
    ["dex", "family", "chain", "network"],
)
dex_quote_fail_total = Counter(
    "mevbot_dex_quote_fail_total",
    "DEX quote failures by reason",
    ["dex", "reason", "family", "chain", "network"],
)
dex_quote_latency_seconds = Histogram(
    "mevbot_dex_quote_latency_seconds",
    "DEX quote latency in seconds",
    ["dex", "family", "chain", "network"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)
dex_build_fail_total = Counter(
    "mevbot_dex_build_fail_total",
    "DEX build failures by reason",
    ["dex", "reason", "family", "chain", "network"],
)
dex_sim_fail_total = Counter(
    "mevbot_dex_sim_fail_total",
    "DEX simulation failures by reason",
    ["dex", "reason", "family", "chain", "network"],
)
dex_route_hops = Histogram(
    "mevbot_dex_route_hops",
    "DEX route hop count",
    ["dex", "family", "chain", "network"],
    buckets=[1, 2, 3, 4, 5, 6, 8, 10],
)
sim_fail_total = Counter(
    "mevbot_sim_fail_total",
    "Simulation failures by reason",
    ["family", "chain", "network", "strategy", "reason"],
)
tx_revert_total = Counter(
    "mevbot_tx_revert_total",
    "Transaction reverts by reason",
    ["family", "chain", "network", "reason"],
)
blocked_by_operator_total = Counter(
    "mevbot_blocked_by_operator_total",
    "Transaction sends blocked by operator control (state/kill-switch)",
    ["family", "chain", "network", "scope", "reason"],
)
stream_events_observed_total = Counter(
    "mevbot_stream_events_observed_total",
    "Redis stream entries observed by runtime probes",
    ["stream", "source"],
)
router_quotes_total = Counter(
    "mevbot_router_quotes_total",
    "Total router quote attempts",
    ["family", "chain", "network", "dex", "result"],
)
router_best_dex_selected_total = Counter(
    "mevbot_router_best_dex_selected_total",
    "Total router best-dex selections",
    ["family", "chain", "network", "dex"],
)
router_best_selected_total = Counter(
    "mevbot_router_best_selected_total",
    "Total router best selections",
    ["family", "chain", "network", "dex"],
)
router_quote_fanout = Histogram(
    "mevbot_router_quote_fanout",
    "Number of DEX packs considered by router per intent",
    ["family", "chain", "network"],
    buckets=[1, 2, 3, 4, 5, 8, 12, 20],
)
router_fanout = Histogram(
    "mevbot_router_fanout",
    "Number of DEX packs considered by router per intent",
    ["family", "chain", "network"],
    buckets=[1, 2, 3, 4, 5, 8, 12, 20],
)
xarb_scans_total = Counter(
    "mevbot_xarb_scans_total",
    "Cross-DEX arbitrage scan attempts by dex pair",
    ["family", "chain", "network", "dex_pair", "result"],
)
xarb_opportunities_total = Counter(
    "mevbot_xarb_opportunities_total",
    "Cross-DEX arbitrage opportunities emitted",
    ["family", "chain", "network", "dex_pair"],
)
xarb_rejected_total = Counter(
    "mevbot_xarb_rejected_total",
    "Cross-DEX arbitrage rejects by reason",
    ["family", "chain", "network", "dex_pair", "reason"],
)
triarb_cycles_evaluated_total = Counter(
    "mevbot_triarb_cycles_evaluated_total",
    "Triangular arbitrage cycles evaluated",
    ["family", "chain", "network", "dex_path", "result"],
)
triarb_cycles_emitted_total = Counter(
    "mevbot_triarb_cycles_emitted_total",
    "Triangular arbitrage cycles emitted as opportunities",
    ["family", "chain", "network", "dex_path"],
)
triarb_compute_seconds = Histogram(
    "mevbot_triarb_compute_seconds",
    "Triangular arbitrage compute time in seconds",
    ["family", "chain", "network"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)
flashloan_used_total = Counter(
    "mevbot_flashloan_used_total",
    "Flashloan-wrapped execution attempts",
    ["family", "chain", "network", "provider"],
)
flashloan_fee_est_usd = Counter(
    "mevbot_flashloan_fee_est_usd",
    "Estimated flashloan fees in USD",
    ["family", "chain", "network", "provider"],
)
new_pairs_seen_total = Counter(
    "mevbot_new_pairs_seen_total",
    "Newly discovered UniV2 pairs / UniV3 pools",
    ["dex", "chain"],
)
risk_allow_total = Counter(
    "mevbot_risk_allow_total",
    "Risk firewall ALLOW decisions",
    ["chain"],
)
risk_watch_total = Counter(
    "mevbot_risk_watch_total",
    "Risk firewall WATCH decisions",
    ["chain"],
)
risk_deny_total = Counter(
    "mevbot_risk_deny_total",
    "Risk firewall DENY decisions",
    ["chain"],
)
sell_sim_fail_total = Counter(
    "mevbot_sell_sim_fail_total",
    "Risk firewall tiny-sell simulation failures",
    ["chain", "reason"],
)
mode_outcomes_total = Counter(
    "mevbot_mode_outcomes_total",
    "Execution outcomes by bot mode",
    ["family", "chain", "network", "mode", "outcome"],
)


_STATE_ENUM = {
    "BOOTING": 1.0,
    "SYNCING": 2.0,
    "READY": 3.0,
    "TRADING": 4.0,
    "PAUSED": 5.0,
    "DEGRADED": 6.0,
    "PANIC": 7.0,
}
_STATE_ENUM_STD = {
    "UNKNOWN": 0.0,
    "PAUSED": 1.0,
    "READY": 2.0,
    "TRADING": 3.0,
    "DEGRADED": 4.0,
    "PANIC": 5.0,
}
_MODE_ENUM_STD = {
    "UNKNOWN": 0.0,
    "DRYRUN": 1.0,
    "PAPER": 2.0,
    "LIVE": 3.0,
}
_STATE_MUTEX = threading.Lock()
_last_runtime_state_labels: tuple[str, str, str] | None = None
_last_runtime_context_labels: tuple[str, str, str, str, str] | None = None
_last_desired_state_labels: tuple[str, str, str] | None = None
_last_desired_context_labels: tuple[str, str, str, str, str] | None = None

_REVERT_REASON_BUCKETS = {
    "nonce_too_low",
    "fee_underpriced",
    "reverted",
    "simulation_fail",
    "rate_limit",
    "timeout",
    "transport",
    "rpc_error",
    "exception",
    "blocked_by_operator",
    "kill_switch",
    "state_not_trading",
    "operator_kill_switch",
    "operator_not_trading",
    "guard",
    "other",
}


def map_revert_reason(reason: str | None) -> str:
    r = _norm(reason, default="other")
    if r in _REVERT_REASON_BUCKETS:
        return r
    if "nonce" in r:
        return "nonce_too_low"
    if "underprice" in r or "fee" in r:
        return "fee_underpriced"
    if "revert" in r:
        return "reverted"
    if "sim" in r:
        return "simulation_fail"
    if "429" in r or "rate" in r:
        return "rate_limit"
    if "timeout" in r:
        return "timeout"
    return "other"


def map_rpc_error_code_bucket(code: str | int | None) -> str:
    raw = str(code or "").strip().lower()
    if not raw:
        return "other"
    if raw in {"429", "rate_limit", "rate_limited"}:
        return "429"
    if raw in {"timeout", "timed_out"}:
        return "timeout"
    if raw in {"connection", "connect", "conn_error"}:
        return "conn"
    if raw.startswith("5") and raw.isdigit():
        return "5xx"
    if raw.startswith("4") and raw.isdigit():
        return "4xx"
    if "rpc" in raw:
        return "rpc_error"
    return "other"


def _remove_gauge_labels(gauge: Gauge, labels: tuple[str, ...]) -> None:
    try:
        gauge.remove(*labels)
    except KeyError:
        pass
    except Exception:
        # Metrics updates must stay best-effort and never break runtime loops.
        log.debug("failed removing stale labels from %s", getattr(gauge, "_name", "gauge"), exc_info=True)


def start_metrics_http_server(port: Optional[int] = None) -> None:
    global _SERVER_STARTED
    if _SERVER_STARTED:
        return
    p = int(port if port is not None else os.getenv("METRICS_PORT", "9100"))
    start_http_server(p)
    _SERVER_STARTED = True
    log.info("metrics exporter listening on :%s/metrics", p)


def seed_default_series(*, family: str, chain: str) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy="default", dex="unknown", provider="unknown")
    fam, ch, network = c["family"], c["chain"], c["network"]
    strategy = c["strategy"]
    dex = c["dex"]
    provider = c["provider"]
    reason = "other"

    tx_sent_total.labels(family=fam, chain=ch, network=network, strategy=strategy).inc(0)
    tx_confirmed_total.labels(family=fam, chain=ch, network=network, strategy=strategy).inc(0)
    tx_failed_total.labels(family=fam, chain=ch, network=network, strategy=strategy, reason=reason).inc(0)
    tx_confirm_latency_seconds.labels(
        family=fam, chain=ch, network=network, strategy=strategy
    ).observe(0.0)
    rpc_latency_seconds.labels(
        family=fam, chain=ch, network=network, provider=provider, method="unknown"
    ).observe(0.0)
    sim_fail_total.labels(family=fam, chain=ch, network=network, strategy=strategy, reason=reason).inc(0)
    tx_revert_total.labels(family=fam, chain=ch, network=network, reason=reason).inc(0)
    blocked_by_operator_total.labels(family=fam, chain=ch, network=network, scope="unknown", reason=reason).inc(0)

    opportunities_seen_total.labels(family=fam, chain=ch, network=network, dex=dex, strategy=strategy).inc(0)
    opportunities_attempted_total.labels(family=fam, chain=ch, network=network, dex=dex, strategy=strategy).inc(0)
    opportunities_filled_total.labels(family=fam, chain=ch, network=network, dex=dex, strategy=strategy).inc(0)
    opportunities_rejected_total.labels(
        family=fam, chain=ch, network=network, strategy=strategy, reason=reason
    ).inc(0)
    opportunities_simulated_total.labels(
        family=fam, chain=ch, network=network, strategy=strategy, dex=dex, outcome="ok"
    ).inc(0)
    opportunities_executed_total.labels(
        family=fam, chain=ch, network=network, strategy=strategy, dex=dex, mode="dryrun"
    ).inc(0)
    opportunity_queue_depth.labels(family=fam, chain=ch, network=network, strategy=strategy).set(0.0)
    tx_sent_by_dex_type_total.labels(
        family=fam, chain=ch, network=network, dex=dex, type="unknown"
    ).inc(0)
    dex_quote_total.labels(dex=dex, family=fam, chain=ch, network=network).inc(0)
    dex_quote_fail_total.labels(dex=dex, reason=reason, family=fam, chain=ch, network=network).inc(0)
    dex_build_fail_total.labels(dex=dex, reason=reason, family=fam, chain=ch, network=network).inc(0)
    dex_sim_fail_total.labels(dex=dex, reason=reason, family=fam, chain=ch, network=network).inc(0)
    dex_quote_latency_seconds.labels(dex=dex, family=fam, chain=ch, network=network).observe(0.0)
    dex_route_hops.labels(dex=dex, family=fam, chain=ch, network=network).observe(1.0)

    pnl_realized_usd.labels(family=fam, chain=ch, network=network, strategy=strategy).set(0.0)
    fees_total_usd.labels(family=fam, chain=ch, network=network, strategy=strategy).set(0.0)
    drawdown_usd.labels(family=fam, chain=ch, network=network, strategy=strategy).set(0.0)
    head_lag_blocks.labels(family=fam, chain=ch, network=network, provider=provider).set(0.0)
    slot_lag.labels(family=fam, chain=ch, network=network, provider=provider).set(0.0)
    chain_head.labels(family=fam, chain=ch, network=network, provider=provider).set(0.0)
    chain_slot.labels(family=fam, chain=ch, network=network, provider=provider).set(0.0)
    heartbeat_ts.labels(
        family=fam,
        chain=ch,
        network=network,
        provider=provider,
        dex=dex,
        strategy=strategy,
    ).set(0.0)
    chain_info.labels(family=fam, chain=ch, network=network).set(1.0)
    rpc_errors_total.labels(family=fam, chain=ch, network=network, provider=provider, code_bucket=reason).inc(0)
    stream_events_observed_total.labels(stream=os.getenv("REDIS_STREAM", "mempool:pending:txs"), source="api_probe").inc(0)
    router_quotes_total.labels(family=fam, chain=ch, network=network, dex=dex, result="ok").inc(0)
    router_best_dex_selected_total.labels(family=fam, chain=ch, network=network, dex=dex).inc(0)
    router_best_selected_total.labels(family=fam, chain=ch, network=network, dex=dex).inc(0)
    router_quote_fanout.labels(family=fam, chain=ch, network=network).observe(1.0)
    router_fanout.labels(family=fam, chain=ch, network=network).observe(1.0)
    xarb_scans_total.labels(family=fam, chain=ch, network=network, dex_pair="unknown", result="ok").inc(0)
    xarb_opportunities_total.labels(family=fam, chain=ch, network=network, dex_pair="unknown").inc(0)
    xarb_rejected_total.labels(family=fam, chain=ch, network=network, dex_pair="unknown", reason=reason).inc(0)
    triarb_cycles_evaluated_total.labels(
        family=fam, chain=ch, network=network, dex_path="unknown", result="ok"
    ).inc(0)
    triarb_cycles_emitted_total.labels(family=fam, chain=ch, network=network, dex_path="unknown").inc(0)
    triarb_compute_seconds.labels(family=fam, chain=ch, network=network).observe(0.0)
    flashloan_used_total.labels(family=fam, chain=ch, network=network, provider="unknown").inc(0)
    flashloan_fee_est_usd.labels(family=fam, chain=ch, network=network, provider="unknown").inc(0)
    new_pairs_seen_total.labels(dex="unknown", chain=ch).inc(0)
    risk_allow_total.labels(chain=ch).inc(0)
    risk_watch_total.labels(chain=ch).inc(0)
    risk_deny_total.labels(chain=ch).inc(0)
    sell_sim_fail_total.labels(chain=ch, reason=reason).inc(0)
    mode_outcomes_total.labels(family=fam, chain=ch, network=network, mode="paper", outcome="virtual_fill").inc(0)


def record_tx_sent(*, family: str, chain: str, strategy: str = "default") -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    tx_sent_total.labels(family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]).inc()


def record_tx_confirmed(*, family: str, chain: str, strategy: str = "default") -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    tx_confirmed_total.labels(family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]).inc()


def record_tx_failed(*, family: str, chain: str, reason: str, strategy: str = "default") -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    tx_failed_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"], reason=_norm(reason)
    ).inc()


def record_tx_confirm_latency(*, family: str, chain: str, seconds: float, strategy: str = "default") -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    tx_confirm_latency_seconds.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]
    ).observe(max(0.0, float(seconds)))


def record_rpc_latency(
    *,
    family: str,
    chain: str,
    provider: str,
    seconds: float,
    method: str = "unknown",
) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    rpc_latency_seconds.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        provider=c["provider"],
        method=_norm(method),
    ).observe(max(0.0, float(seconds)))


def record_rpc_error(*, provider: str, code_bucket: str | int | None, family: str = "evm", chain: str = "unknown") -> None:
    if chain == "unknown":
        chain = str(os.getenv("CHAIN", "unknown")).strip() or "unknown"
    if family in {"", "unknown"}:
        family = str(os.getenv("CHAIN_FAMILY", "evm")).strip() or "evm"
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    rpc_errors_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        provider=c["provider"],
        code_bucket=map_rpc_error_code_bucket(code_bucket),
    ).inc()


def set_runtime_bot_state(*, family: str, chain: str, state: str, mode: str | None = None) -> None:
    c = _canon_ctx(family=family, chain=chain)
    s = str(state).upper()
    runtime_labels = (c["family"], c["chain"], c["network"])
    mode_norm = _norm(mode if mode is not None else os.getenv("MODE"), default="unknown")
    context_labels = (c["family"], c["chain"], c["network"], s, mode_norm.upper())

    global _last_runtime_state_labels, _last_runtime_context_labels
    with _STATE_MUTEX:
        if _last_runtime_state_labels and _last_runtime_state_labels != runtime_labels:
            _remove_gauge_labels(runtime_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(effective_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(state_gauge, _last_runtime_state_labels)
            _remove_gauge_labels(bot_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(chain_info, _last_runtime_state_labels)
            _remove_gauge_labels(effective_chain_info, _last_runtime_state_labels)
            _remove_gauge_labels(effective_mode_info, _last_runtime_state_labels)
        if _last_runtime_context_labels and _last_runtime_context_labels != context_labels:
            _remove_gauge_labels(runtime_context_info, _last_runtime_context_labels)
        _last_runtime_state_labels = runtime_labels
        _last_runtime_context_labels = context_labels

    state_metric_value = _STATE_ENUM_STD.get(s, _STATE_ENUM_STD["UNKNOWN"])
    runtime_state_value.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(state_metric_value)
    effective_state_value.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(state_metric_value)
    # Backward-compat aliases kept temporarily.
    bot_state_value.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(_STATE_ENUM.get(s, 0.0))
    state_gauge.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(state_metric_value)
    chain_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(1.0)
    effective_chain_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(1.0)
    effective_mode_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(
        _MODE_ENUM_STD.get(mode_norm.upper(), _MODE_ENUM_STD["UNKNOWN"])
    )
    runtime_context_info.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        state=s,
        mode=mode_norm.upper(),
    ).set(1.0)


def set_desired_bot_state(
    *,
    family: str,
    chain_target: str | None = None,
    chain: str | None = None,
    state: str,
    mode: str = "unknown",
) -> None:
    c = _canon_ctx(family=family, chain=(chain if chain is not None else chain_target))
    s = str(state).upper()
    m = _norm(mode, default="unknown")
    desired_labels = (c["family"], c["chain"], c["network"])
    desired_context_labels = (c["family"], c["chain"], c["network"], s, m.upper())

    global _last_desired_state_labels, _last_desired_context_labels
    with _STATE_MUTEX:
        if _last_desired_state_labels and _last_desired_state_labels != desired_labels:
            _remove_gauge_labels(desired_state_value, _last_desired_state_labels)
            _remove_gauge_labels(desired_chain_info, _last_desired_state_labels)
            _remove_gauge_labels(desired_mode_info, _last_desired_state_labels)
        if _last_desired_context_labels and _last_desired_context_labels != desired_context_labels:
            _remove_gauge_labels(desired_context_info, _last_desired_context_labels)
        _last_desired_state_labels = desired_labels
        _last_desired_context_labels = desired_context_labels

    desired_state_value.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).set(_STATE_ENUM_STD.get(s, _STATE_ENUM_STD["UNKNOWN"]))
    desired_chain_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(1.0)
    desired_mode_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(
        _MODE_ENUM_STD.get(m.upper(), _MODE_ENUM_STD["UNKNOWN"])
    )
    desired_context_info.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        state=s,
        mode=m.upper(),
    ).set(1.0)


def set_head_lag(*, family: str, chain: str, provider: str, blocks: float) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    head_lag_blocks.labels(
        family=c["family"], chain=c["chain"], network=c["network"], provider=c["provider"]
    ).set(float(blocks))


def set_slot_lag(*, family: str, chain: str, provider: str, lag: float) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    slot_lag.labels(
        family=c["family"], chain=c["chain"], network=c["network"], provider=c["provider"]
    ).set(float(lag))


def set_heartbeat(
    *,
    family: str,
    chain: str,
    unix_ts: float,
    provider: str = "unknown",
    dex: str = "unknown",
    strategy: str = "default",
) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider, dex=dex, strategy=strategy)
    heartbeat_ts.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        provider=c["provider"],
        dex=c["dex"],
        strategy=c["strategy"],
    ).set(float(unix_ts))
    chain_info.labels(family=c["family"], chain=c["chain"], network=c["network"]).set(1.0)


def set_chain_head(*, family: str, chain: str, provider: str, height: int | float) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    chain_head.labels(
        family=c["family"], chain=c["chain"], network=c["network"], provider=c["provider"]
    ).set(float(height))


def set_chain_slot(*, family: str, chain: str, provider: str, slot: int | float) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    chain_slot.labels(
        family=c["family"], chain=c["chain"], network=c["network"], provider=c["provider"]
    ).set(float(slot))


def set_pnl_realized(*, family: str, chain: str, strategy: str, usd: float) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    pnl_realized_usd.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]
    ).set(float(usd))


def set_fees_total(*, family: str, chain: str, strategy: str, usd: float) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    fees_total_usd.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]
    ).set(float(usd))


def set_fees_paid(*, family: str, chain: str, strategy: str, usd: float) -> None:
    # Backward-compatible alias used by older call sites.
    set_fees_total(family=family, chain=chain, strategy=strategy, usd=usd)


def set_drawdown(*, family: str, chain: str, strategy: str, usd: float) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    drawdown_usd.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"]
    ).set(float(usd))


def record_opportunity_seen(*, family: str, chain: str, dex: str, strategy: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex, strategy=strategy)
    opportunities_seen_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], dex=c["dex"], strategy=c["strategy"]
    ).inc()


def record_opportunity_attempted(*, family: str, chain: str, dex: str, strategy: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex, strategy=strategy)
    opportunities_attempted_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], dex=c["dex"], strategy=c["strategy"]
    ).inc()


def record_opportunity_filled(*, family: str, chain: str, dex: str, strategy: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex, strategy=strategy)
    opportunities_filled_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], dex=c["dex"], strategy=c["strategy"]
    ).inc()


def record_opportunity_rejected(*, family: str, chain: str, strategy: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    opportunities_rejected_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        strategy=c["strategy"],
        reason=map_revert_reason(reason),
    ).inc()


def record_opportunity_simulated(
    *,
    family: str,
    chain: str,
    strategy: str,
    dex: str,
    ok: bool,
) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy, dex=dex)
    opportunities_simulated_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        strategy=c["strategy"],
        dex=c["dex"],
        outcome="ok" if ok else "fail",
    ).inc()


def record_opportunity_executed(
    *,
    family: str,
    chain: str,
    strategy: str,
    dex: str,
    mode: str,
) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy, dex=dex)
    opportunities_executed_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        strategy=c["strategy"],
        dex=c["dex"],
        mode=_norm(mode, default="unknown"),
    ).inc()


def set_opportunity_queue_depth(*, family: str, chain: str, strategy: str, depth: int | float) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    opportunity_queue_depth.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        strategy=c["strategy"],
    ).set(max(0.0, float(depth)))


def record_tx_sent_by_dex_type(*, family: str, chain: str, dex: str, tx_type: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    tx_sent_by_dex_type_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex=c["dex"],
        type=_norm(tx_type, default="unknown"),
    ).inc()


def record_dex_quote(*, family: str, chain: str, dex: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_quote_total.labels(dex=c["dex"], family=c["family"], chain=c["chain"], network=c["network"]).inc()


def record_dex_quote_fail(*, family: str, chain: str, dex: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_quote_fail_total.labels(
        dex=c["dex"],
        reason=map_revert_reason(reason),
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).inc()


def record_dex_quote_latency(*, family: str, chain: str, dex: str, seconds: float) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_quote_latency_seconds.labels(
        dex=c["dex"],
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).observe(max(0.0, float(seconds)))


def record_dex_build_fail(*, family: str, chain: str, dex: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_build_fail_total.labels(
        dex=c["dex"],
        reason=map_revert_reason(reason),
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).inc()


def record_dex_sim_fail(*, family: str, chain: str, dex: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_sim_fail_total.labels(
        dex=c["dex"],
        reason=map_revert_reason(reason),
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).inc()


def record_dex_route_hops(*, family: str, chain: str, dex: str, hops: int | float) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    dex_route_hops.labels(
        dex=c["dex"],
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).observe(max(1.0, float(hops)))


def record_opportunity_filtered(*, family: str, chain: str, strategy: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    # Reuse standardized failure counter with reason labels for filtered opportunities.
    sim_fail_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"], reason=map_revert_reason(reason)
    ).inc()


def record_sim_fail(*, family: str, chain: str, strategy: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain, strategy=strategy)
    sim_fail_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], strategy=c["strategy"], reason=_norm(reason)
    ).inc()


def record_tx_revert(*, family: str, chain: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain)
    tx_revert_total.labels(
        family=c["family"], chain=c["chain"], network=c["network"], reason=map_revert_reason(reason)
    ).inc()


def record_stream_events_observed(*, stream: str, count: int | float = 1, source: str = "api_probe") -> None:
    n = max(0.0, float(count))
    if n <= 0:
        return
    stream_events_observed_total.labels(stream=str(stream or "unknown"), source=_norm(source)).inc(n)


def record_blocked_by_operator(*, scope: str, chain: str, reason: str) -> None:
    c = _canon_ctx(family=None, chain=chain)
    blocked_by_operator_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        scope=_norm(scope),
        reason=map_revert_reason(reason),
    ).inc()


def record_router_quote(*, family: str, chain: str, dex: str, ok: bool) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    router_quotes_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex=c["dex"],
        result="ok" if ok else "fail",
    ).inc()


def record_router_best_dex_selected(*, family: str, chain: str, dex: str) -> None:
    c = _canon_ctx(family=family, chain=chain, dex=dex)
    router_best_dex_selected_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex=c["dex"],
    ).inc()
    router_best_selected_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex=c["dex"],
    ).inc()


def record_router_quote_fanout(*, family: str, chain: str, fanout: int | float) -> None:
    c = _canon_ctx(family=family, chain=chain)
    router_quote_fanout.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).observe(max(0.0, float(fanout)))
    router_fanout.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).observe(max(0.0, float(fanout)))


def record_xarb_scan(*, family: str, chain: str, dex_pair: str, ok: bool) -> None:
    c = _canon_ctx(family=family, chain=chain)
    xarb_scans_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex_pair=_norm(dex_pair),
        result="ok" if ok else "fail",
    ).inc()


def record_xarb_opportunity(*, family: str, chain: str, dex_pair: str) -> None:
    c = _canon_ctx(family=family, chain=chain)
    xarb_opportunities_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex_pair=_norm(dex_pair),
    ).inc()


def record_xarb_reject(*, family: str, chain: str, dex_pair: str, reason: str) -> None:
    c = _canon_ctx(family=family, chain=chain)
    xarb_rejected_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex_pair=_norm(dex_pair),
        reason=_norm(reason),
    ).inc()


def record_triarb_cycle_evaluated(*, family: str, chain: str, dex_path: str, ok: bool) -> None:
    c = _canon_ctx(family=family, chain=chain)
    triarb_cycles_evaluated_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex_path=_norm(dex_path),
        result="ok" if ok else "fail",
    ).inc()


def record_triarb_cycle_emitted(*, family: str, chain: str, dex_path: str) -> None:
    c = _canon_ctx(family=family, chain=chain)
    triarb_cycles_emitted_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        dex_path=_norm(dex_path),
    ).inc()


def record_triarb_compute_time(*, family: str, chain: str, seconds: float) -> None:
    c = _canon_ctx(family=family, chain=chain)
    triarb_compute_seconds.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
    ).observe(max(0.0, float(seconds)))


def record_flashloan_used(*, family: str, chain: str, provider: str) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    flashloan_used_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        provider=c["provider"],
    ).inc()


def record_flashloan_fee_est_usd(*, family: str, chain: str, provider: str, usd: float) -> None:
    c = _canon_ctx(family=family, chain=chain, provider=provider)
    flashloan_fee_est_usd.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        provider=c["provider"],
    ).inc(max(0.0, float(usd)))


def record_new_pair_seen(*, dex: str, chain: str) -> None:
    new_pairs_seen_total.labels(
        dex=_norm(dex),
        chain=_norm(chain),
    ).inc()


def record_risk_allow(*, chain: str) -> None:
    risk_allow_total.labels(chain=_norm(chain)).inc()


def record_risk_watch(*, chain: str) -> None:
    risk_watch_total.labels(chain=_norm(chain)).inc()


def record_risk_deny(*, chain: str) -> None:
    risk_deny_total.labels(chain=_norm(chain)).inc()


def record_sell_sim_fail(*, chain: str, reason: str) -> None:
    sell_sim_fail_total.labels(
        chain=_norm(chain),
        reason=_norm(reason, default="unknown"),
    ).inc()


def record_mode_outcome(*, family: str, chain: str, mode: str, outcome: str) -> None:
    c = _canon_ctx(family=family, chain=chain)
    m = _norm(mode, default="unknown")
    o = _norm(outcome, default="unknown")
    mode_outcomes_total.labels(
        family=c["family"],
        chain=c["chain"],
        network=c["network"],
        mode=m,
        outcome=o,
    ).inc()


def record_tx_result(
    *,
    family: str,
    chain: str,
    ok: bool,
    strategy: str = "default",
    reason: str = "other",
    confirm_latency_s: float | None = None,
) -> None:
    if ok:
        record_tx_confirmed(family=family, chain=chain, strategy=strategy)
    else:
        record_tx_failed(family=family, chain=chain, reason=reason, strategy=strategy)
        record_tx_revert(family=family, chain=chain, reason=reason)
    if confirm_latency_s is not None:
        record_tx_confirm_latency(
            family=family,
            chain=chain,
            strategy=strategy,
            seconds=float(confirm_latency_s),
        )


def _reset_state_gauges_for_tests() -> None:
    global _last_runtime_state_labels, _last_runtime_context_labels
    global _last_desired_state_labels, _last_desired_context_labels
    with _STATE_MUTEX:
        if _last_runtime_state_labels:
            _remove_gauge_labels(runtime_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(effective_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(state_gauge, _last_runtime_state_labels)
            _remove_gauge_labels(bot_state_value, _last_runtime_state_labels)
            _remove_gauge_labels(chain_info, _last_runtime_state_labels)
            _remove_gauge_labels(effective_chain_info, _last_runtime_state_labels)
            _remove_gauge_labels(effective_mode_info, _last_runtime_state_labels)
        if _last_runtime_context_labels:
            _remove_gauge_labels(runtime_context_info, _last_runtime_context_labels)
        if _last_desired_state_labels:
            _remove_gauge_labels(desired_state_value, _last_desired_state_labels)
            _remove_gauge_labels(desired_chain_info, _last_desired_state_labels)
            _remove_gauge_labels(desired_mode_info, _last_desired_state_labels)
        if _last_desired_context_labels:
            _remove_gauge_labels(desired_context_info, _last_desired_context_labels)
        _last_runtime_state_labels = None
        _last_runtime_context_labels = None
        _last_desired_state_labels = None
        _last_desired_context_labels = None


# Backward-compatible alias expected by some callers/tests.
fees_paid_usd = fees_total_usd
state = state_gauge

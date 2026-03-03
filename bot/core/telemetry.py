import contextlib
import logging
import os
import shutil
import time
from pathlib import Path
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from bot.core.canonical import ctx_labels
from bot.core.state_machine import ALL_BOT_STATES

log = logging.getLogger("telemetry")
_MULTIPROC_INIT_DONE = False


def _as_bool(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_worker_count() -> int:
    keys = ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS", "PROMETHEUS_WORKERS")
    vals = []
    for k in keys:
        raw = str(os.getenv(k, "")).strip()
        if not raw:
            continue
        try:
            vals.append(int(raw))
        except Exception:
            continue
    return max(vals) if vals else 1


def _clear_multiproc_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    backup = path.parent / f"{path.name}.corrupt.{int(time.time())}"
    try:
        if path.exists():
            path.rename(backup)
    except Exception:
        for child in path.glob("*"):
            with contextlib.suppress(Exception):
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        path.mkdir(parents=True, exist_ok=True)
        return
    path.mkdir(parents=True, exist_ok=True)


def ensure_prometheus_multiproc_ready(*, force: bool = False) -> bool:
    """
    Best-effort guard for corrupted PROMETHEUS_MULTIPROC_DIR shard files.
    Returns True when multiprocess mode remains enabled, False otherwise.
    """
    global _MULTIPROC_INIT_DONE
    if _MULTIPROC_INIT_DONE and not force:
        return bool(os.getenv("PROMETHEUS_MULTIPROC_DIR"))

    mp_dir = str(os.getenv("PROMETHEUS_MULTIPROC_DIR", "")).strip()
    if not mp_dir:
        _MULTIPROC_INIT_DONE = True
        return False

    # Prefer single-process metrics in our default runtime unless explicitly required.
    if _configured_worker_count() <= 1 and not _as_bool(os.getenv("PROMETHEUS_MULTIPROC_REQUIRED")):
        log.info(
            "PROMETHEUS_MULTIPROC_DIR is set (%s) but worker count <=1; disabling multiprocess metrics. "
            "Set PROMETHEUS_MULTIPROC_REQUIRED=1 only for multi-worker setups.",
            mp_dir,
        )
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
        _MULTIPROC_INIT_DONE = True
        return False

    mp_path = Path(mp_dir)
    mp_path.mkdir(parents=True, exist_ok=True)
    try:
        from prometheus_client import CollectorRegistry, multiprocess

        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        # Probe scrape build to trigger mmap/decode errors early.
        generate_latest(reg)
    except (UnicodeDecodeError, OSError, ValueError, RuntimeError) as e:
        log.warning(
            "Prometheus multiprocess shard files appear corrupted in %s: %s. "
            "Resetting directory and continuing startup.",
            mp_dir,
            e,
        )
        _clear_multiproc_dir(mp_path)
    _MULTIPROC_INIT_DONE = True
    return bool(os.getenv("PROMETHEUS_MULTIPROC_DIR"))


def _chain_labels(chain: str | None = None, chain_family: str | None = None) -> tuple[str, str]:
    ctx = ctx_labels(
        family=(chain_family or os.getenv("CHAIN_FAMILY") or "evm"),
        chain=(chain or os.getenv("CHAIN") or "unknown"),
    )
    fam, ch = ctx["family"], ctx["chain"]
    return fam, ch


def _chain_label_values(chain: str | None = None, chain_family: str | None = None) -> dict[str, str]:
    ctx = ctx_labels(
        family=(chain_family or os.getenv("CHAIN_FAMILY") or "evm"),
        chain=(chain or os.getenv("CHAIN") or "unknown"),
    )
    return {"family": ctx["family"], "chain": ctx["chain"], "network": ctx["network"]}


def canonical_metric_labels(chain: str | None = None, chain_family: str | None = None) -> dict[str, str]:
    return _chain_label_values(chain=chain, chain_family=chain_family)


def get_ctx_labels(chain: str | None = None, chain_family: str | None = None) -> dict[str, str]:
    """Canonical low-cardinality labels for chain context."""
    return canonical_metric_labels(chain=chain, chain_family=chain_family)


def get_endpoint_labels(
    endpoint_alias: str,
    *,
    chain: str | None = None,
    chain_family: str | None = None,
) -> dict[str, str]:
    """Canonical labels for endpoint-scoped mempool metrics."""
    labels = canonical_metric_labels(chain=chain, chain_family=chain_family)
    labels["endpoint"] = str(endpoint_alias or "unknown")
    return labels

# Initialize multiprocess shard handling before metric objects are created.
with contextlib.suppress(Exception):
    ensure_prometheus_multiproc_ready()

# ---------- Mempool ----------
mempool_rx_total = Counter(
    "mevbot_mempool_rx_total",
    "Total raw pending-tx notifications received per endpoint",
    ["family", "chain", "network", "endpoint"]
)
mempool_rx_errors_total = Counter(
    "mevbot_mempool_rx_errors_total",
    "Total WS errors by endpoint",
    ["family", "chain", "network", "endpoint"]
)
mempool_reconnects_total = Counter(
    "mevbot_mempool_reconnects_total",
    "Total reconnects by endpoint",
    ["family", "chain", "network", "endpoint"]
)
mempool_ws_connected = Gauge(
    "mevbot_mempool_ws_connected",
    "WebSocket connection state (0/1) per endpoint",
    ["family", "chain", "network", "endpoint"]
)

mempool_unique_tx_total = Counter(
    "mevbot_mempool_unique_tx_total",
    "Total unique pending tx observed (dedup across endpoints)",
    ["family", "chain", "network"]
)
mempool_pending_tx_total = Counter(
    "mempool_pending_tx_total",
    "Pending tx count by chain",
    ["family", "chain", "network"]
)
mempool_tps = Gauge(
    "mevbot_mempool_tps",
    "Mempool transactions per second (rolling 60s)",
    ["family", "chain", "network"],
)
mempool_tpm = Gauge(
    "mevbot_mempool_tpm",
    "Mempool transactions per minute (rolling 60s)",
    ["family", "chain", "network"],
)
# Backwards-compatibility for one release cycle.
mempool_tps_legacy = Gauge(
    "mevbot_mempool_tps_legacy",
    "DEPRECATED: unlabeled mempool TPS gauge; use mevbot_mempool_tps{family,chain,network}",
    ["family", "chain", "network"],
)
mempool_tpm_legacy = Gauge(
    "mevbot_mempool_tpm_legacy",
    "DEPRECATED: unlabeled mempool TPM gauge; use mevbot_mempool_tpm{family,chain,network}",
    ["family", "chain", "network"],
)

mempool_message_latency_ms = Histogram(
    "mevbot_mempool_message_latency_ms",
    "Time from WS recv -> enqueue/process",
    ["family", "chain", "network", "endpoint"],
    buckets=[1,2,5,10,20,50,100,250,500,1000]
)
mempool_message_latency_ms_legacy = Histogram(
    "mevbot_mempool_message_latency_ms_legacy",
    "DEPRECATED: unlabeled mempool message latency; use labeled metric",
    ["family", "chain", "network", "endpoint"],
    buckets=[1,2,5,10,20,50,100,250,500,1000],
)

mempool_stream_publish_total = Counter(
    "mevbot_mempool_stream_publish_total",
    "Total tx hashes published to Redis stream",
    ["family", "chain", "network", "stream"]
)
mempool_stream_publish_errors_total = Counter(
    "mevbot_mempool_stream_publish_errors_total",
    "Total Redis publish errors",
    ["family", "chain", "network", "stream"]
)
mempool_stream_consume_total = Counter(
    "mevbot_mempool_stream_consume_total",
    "Entries consumed from Redis stream",
    ["family", "chain", "network", "stream"]
)
mempool_stream_consume_errors_total = Counter(
    "mevbot_mempool_stream_consume_errors_total",
    "Consumer-side stream processing errors",
    ["family", "chain", "network", "stream", "kind"]
)
mempool_consumer_throughput_tps = Gauge(
    "mevbot_mempool_consumer_throughput_tps",
    "Rolling consumer throughput in transactions/second",
    ["family", "chain", "network", "stream", "consumer"]
)
mempool_stream_consume_lag_ms = Histogram(
    "mevbot_mempool_stream_consume_lag_ms",
    "Lag between published ts and now (ms)",
    ["family", "chain", "network", "endpoint"],
    buckets=[10,50,100,250,500,1000,2000,5000,10000]
)
mempool_stream_consume_lag_ms_legacy = Histogram(
    "mevbot_mempool_stream_consume_lag_ms_legacy",
    "DEPRECATED: unlabeled stream consume lag; use labeled metric",
    ["family", "chain", "network", "endpoint"],
    buckets=[10,50,100,250,500,1000,2000,5000,10000],
)
mempool_stream_xlen = Gauge(
    "mevbot_mempool_stream_xlen",
    "Current Redis stream length (XLEN)",
    ["family", "chain", "network", "stream"]
)
mempool_stream_group_lag = Gauge(
    "mevbot_mempool_stream_group_lag",
    "Approximate Redis stream group lag (entries-read minus last-delivered)",
    ["family", "chain", "network", "stream", "group"]
)
mempool_dlq_writes_total = Counter(
    "mevbot_mempool_dlq_writes_total",
    "DLQ write attempts by result",
    ["family", "chain", "network", "stream", "dlq_stream", "result"]
)
candidate_pipeline_seen_total = Counter(
    "mevbot_candidate_pipeline_seen_total",
    "Total stream entries seen by candidate pipeline",
    ["family", "chain", "network"]
)
candidate_pipeline_detected_total = Counter(
    "mevbot_candidate_pipeline_detected_total",
    "Total candidates that matched first detector",
    ["family", "chain", "network", "kind"]
)
candidate_pipeline_decisions_total = Counter(
    "mevbot_candidate_pipeline_decisions_total",
    "Candidate pipeline decisions",
    ["family", "chain", "network", "decision", "reason"]
)

rpc_gettx_ok_total = Counter("mevbot_rpc_gettx_ok_total","Successful eth_getTransactionByHash calls")
rpc_gettx_errors_total = Counter("mevbot_rpc_gettx_errors_total","Failed eth_getTransactionByHash calls")
rpc_gettx_429_total = Counter("mevbot_rpc_gettx_429_total","HTTP 429 responses from eth_getTransactionByHash calls")
rpc_rate_limit_waits_total = Counter("mevbot_rpc_rate_limit_waits_total","Token-bucket waits before RPC call")
rpc_circuit_breaker_trips_total = Counter("mevbot_rpc_circuit_breaker_trips_total","Circuit-breaker open events due to high 429 ratio")
rpc_circuit_breaker_open = Gauge("mevbot_rpc_circuit_breaker_open","RPC circuit-breaker state (1=open,0=closed)")
rpc_429_ratio = Gauge("mevbot_rpc_429_ratio","Rolling ratio of HTTP 429 responses")
dex_tx_detected_total = Counter("mevbot_dex_tx_detected_total","Transactions that look like DEX swaps")

# ---------- Hunter ----------
backrun_candidates_total = Counter(
    "mevbot_backrun_candidates_total",
    "Backrun candidates emitted by detector",
    ["chain","dex","pool_fee_bps"]
)
backrun_opportunities_total = Counter(
    "mevbot_backrun_opportunities_total",
    "Backrun opportunities scored as viable",
    ["chain","dex","pool_fee_bps"]
)
backrun_rejected_total = Counter(
    "mevbot_backrun_rejected_total",
    "Backrun opportunities rejected by filters",
    ["chain","reason"]
)
backrun_est_profit_usd = Histogram(
    "mevbot_backrun_est_profit_usd",
    "Estimated net profit (USD) for viable backruns",
    ["chain","dex","pool_fee_bps"],
    buckets=[0.5,1,2,5,10,25,50,100,250,500,1000]
)

# ---------- Orderflow / Relays ----------
orderflow_submit_total = Counter(
    "mevbot_orderflow_submit_total",
    "Total JSON-RPC submissions to private endpoints",
    ["endpoint","method","chain"]
)
orderflow_submit_success_total = Counter(
    "mevbot_orderflow_submit_success_total",
    "Successful submissions by endpoint/method",
    ["endpoint","method","chain"]
)
orderflow_submit_fail_total = Counter(
    "mevbot_orderflow_submit_fail_total",
    "Failed submissions by endpoint/method/kind",
    ["endpoint","method","chain","kind"]  # kind: rpc_error|transport
)
orderflow_submit_latency_ms = Histogram(
    "mevbot_orderflow_submit_latency_ms",
    "Submission latency in ms by endpoint/method",
    ["endpoint","method","chain"],
    buckets=[5,10,20,50,100,200,500,1000,2000,5000]
)
orderflow_endpoint_healthy = Gauge(
    "mevbot_orderflow_endpoint_healthy",
    "Endpoint health (1=recent success, 0=recent failure)",
    ["endpoint","chain"]
)

sim_single_total = Counter("mevbot_sim_single_total","Total single-tx simulations attempted")
sim_single_success_total = Counter("mevbot_sim_single_success_total","Successful single-tx sims")
sim_single_fail_total = Counter("mevbot_sim_single_fail_total","Failed single-tx sims",["kind"])

sim_bundle_total = Counter("mevbot_sim_bundle_total","Total bundle simulations attempted")
sim_bundle_success_total = Counter("mevbot_sim_bundle_success_total","Successful bundle sims")
sim_bundle_fail_total = Counter("mevbot_sim_bundle_fail_total","Failed bundle sims",["kind"])

private_submit_attempts = Counter(
    "mevbot_private_submit_attempts_total",
    "Attempts to submit a private tx",
    ["relay","chain","reason"]  # reason: selected|retry|fallback
)
private_submit_success = Counter(
    "mevbot_private_submit_success_total",
    "Successful private tx submissions",
    ["relay","chain"]
)
private_submit_errors = Counter(
    "mevbot_private_submit_errors_total",
    "Errors during private tx submission",
    ["relay","chain","code"]     # code: timeout|rpc_error|rate_limited|bad_nonce|other
)
private_submit_latency_ms = Histogram(
    "mevbot_private_submit_latency_ms",
    "Private submission latency",
    ["relay","chain"],
    buckets=[10,20,50,100,200,500,1000,2000,5000]
)
private_route_decisions = Counter(
    "mevbot_private_route_decisions_total",
    "Router decisions for outgoing trades",
    ["mode","chain","decision"]  # decision: flashbots|mev_blocker|cow|public
)

relay_attempts_total = Counter("mevbot_relay_attempts_total","Relay submission attempts",["relay","chain"])
relay_success_total  = Counter("mevbot_relay_success_total","Relay submission successes",["relay","chain"])
relay_fail_total     = Counter("mevbot_relay_fail_total","Relay submission failures by reason",["relay","chain","reason"])
relay_success_ratio  = Gauge ("mevbot_relay_success_ratio","Successes/Attempts per relay",["relay","chain"])
relay_latency_ms     = Histogram("mevbot_relay_latency_ms","End-to-end submission latency",["relay","chain"],buckets=[5,10,20,50,100,200,500,1000])

# ---------- Stealth / Orchestration / Risk ----------
stealth_trigger_flags_total = Counter(
    "mevbot_stealth_trigger_flags_total",
    "Counts of individual flags that fired for stealth decision",
    ["flag"]
)
stealth_decisions_total = Counter(
    "stealth_decisions_total","",["decision"], namespace="mevbot"
)
stealth_flags_count = Gauge("mevbot_stealth_flags_count","Number of flags that fired in last decision")

orchestrator_decisions_total = Counter(
    "orchestrator_decisions_total","",["mode","reason"], namespace="mevbot"
)
risk_blocks_total = Counter("mevbot_risk_blocks_total","Trades blocked by risk gate",["reason"])
risk_state_gauge = Gauge("mevbot_risk_state","Risk state gauges (value depends on key)",["key"])
bot_state_gauge = Gauge(
    "mevbot_bot_state",
    "DEPRECATED: use mevbot_state{family,chain} enum gauge instead",
    ["state"],
)
bot_state_transitions_total = Counter(
    "mevbot_bot_state_transitions_total",
    "Lifecycle state transitions",
    ["from_state", "to_state", "reason"],
)
exec_guard_blocks_total = Counter(
    "mevbot_exec_guard_blocks_total",
    "Execution attempts blocked by feature guard",
    ["scope", "reason"],
)
# Generic/interop metric names requested by SRE dashboards.
bot_state = Gauge(
    "bot_state",
    "DEPRECATED: use mevbot_state{family,chain} enum gauge instead",
    ["state", "chain_family", "chain"],
)
trades_sent_total = Counter(
    "trades_sent_total",
    "Total successfully submitted trades",
    ["chain_family", "chain"],
)
trades_failed_total = Counter(
    "trades_failed_total",
    "Total failed trade submissions",
    ["chain_family", "chain", "reason"],
)
blocked_by_operator_total = Counter(
    "blocked_by_operator_total",
    "Total transaction sends blocked by operator state/kill switch",
    ["scope", "chain", "reason"],
)
rpc_latency_seconds = Histogram(
    "rpc_latency_seconds",
    "RPC call latency in seconds",
    ["chain_family", "chain", "endpoint", "method"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

# ---------- Builders ----------
bundle_attempts_total = Counter("mevbot_bundle_attempts_total","Bundle submission attempts",["builder","chain"])
bundle_inclusions_total = Counter("mevbot_bundle_inclusions_total","Bundles included on-chain",["builder","chain"])
bundle_rejections_total = Counter("mevbot_bundle_rejections_total","Bundle rejections by reason",["builder","chain","reason"])
bundle_inclusion_latency_ms = Histogram(
    "mevbot_bundle_inclusion_latency_ms","Latency from submit → inclusion response",
    ["builder","chain"], buckets=[20,50,100,200,400,800,1200,2000,4000]
)
builder_success_ratio = Gauge("mevbot_builder_success_ratio","Inclusion / Attempts per builder",["builder","chain"])

# ---------- Detectors ----------
detector_predictions_total = Counter("mevbot_detector_predictions_total","Detector predictions by class",["detector","predicted"])
detector_confusion_total = Counter("mevbot_detector_confusion_total","Confusion matrix counts",["detector","actual","predicted"])
detector_precision = Gauge("mevbot_detector_precision","Precision (TP / (TP+FP))",["detector"])
detector_recall = Gauge("mevbot_detector_recall","Recall (TP / (TP+FN))",["detector"])
detector_false_positive_rate = Gauge("mevbot_detector_false_positive_rate","FPR (FP / (FP+TN))",["detector"])

def seed_zeroes():
    # Pre-create labeled series at 0 so Grafana/Prometheus see them immediately.
    for d in ("go","no_go"):
        stealth_decisions_total.labels(decision=d).inc(0)
    for m in ("stealth","hunter","hybrid"):
        for r in ("ok","risk_block","no_opp","error"):
            orchestrator_decisions_total.labels(mode=m, reason=r).inc(0)
    for r in ("rate_limit","fee","simulation","timeout","temporary","unknown"):
        relay_fail_total.labels(relay="any", chain="any", reason=r).inc(0)
    for state in ALL_BOT_STATES:
        bot_state_gauge.labels(state=state).set(0)
        fam, ch = _chain_labels()
        bot_state.labels(state=state, chain_family=fam, chain=ch).set(0)
    chain_labels = canonical_metric_labels()
    mempool_tps.labels(**chain_labels).set(0)
    mempool_tpm.labels(**chain_labels).set(0)
    mempool_tps_legacy.labels(**chain_labels).set(0)
    mempool_tpm_legacy.labels(**chain_labels).set(0)


def set_bot_state(state: str, *, chain: str | None = None, chain_family: str | None = None) -> None:
    current = str(state).upper()
    fam, ch = _chain_labels(chain, chain_family)
    for s in ALL_BOT_STATES:
        bot_state_gauge.labels(state=s).set(1 if s == current else 0)
        bot_state.labels(state=s, chain_family=fam, chain=ch).set(1 if s == current else 0)


def record_bot_state_transition(from_state: str, to_state: str, reason: str) -> None:
    bot_state_transitions_total.labels(
        from_state=str(from_state).upper(),
        to_state=str(to_state).upper(),
        reason=str(reason),
    ).inc()
    set_bot_state(str(to_state))


def record_trade_sent(*, chain: str | None = None, chain_family: str | None = None) -> None:
    fam, ch = _chain_labels(chain, chain_family)
    trades_sent_total.labels(chain_family=fam, chain=ch).inc()


def record_trade_failed(*, reason: str, chain: str | None = None, chain_family: str | None = None) -> None:
    fam, ch = _chain_labels(chain, chain_family)
    trades_failed_total.labels(chain_family=fam, chain=ch, reason=str(reason)).inc()


def observe_rpc_latency(
    *,
    endpoint: str,
    method: str,
    seconds: float,
    chain: str | None = None,
    chain_family: str | None = None,
) -> None:
    fam, ch = _chain_labels(chain, chain_family)
    rpc_latency_seconds.labels(
        chain_family=fam,
        chain=ch,
        endpoint=str(endpoint),
        method=str(method),
    ).observe(max(0.0, float(seconds)))


# ---- Compat symbols expected by older tests ----
PENDING_TX_TOTAL = mempool_pending_tx_total

def mount_metrics(app):
    from fastapi import Response
    @app.get("/metrics")
    def _metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

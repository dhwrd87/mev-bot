# bot/core/telemetry.py
from prometheus_client import Counter, Gauge, Histogram

# Per-WS endpoint metrics
mempool_rx_total = Counter(
    "mevbot_mempool_rx_total",
    "Total raw pending-tx notifications received per endpoint",
    ["endpoint"]
)
mempool_rx_errors_total = Counter(
    "mevbot_mempool_rx_errors_total",
    "Total WS errors by endpoint",
    ["endpoint"]
)
mempool_reconnects_total = Counter(
    "mevbot_mempool_reconnects_total",
    "Total reconnects by endpoint",
    ["endpoint"]
)
mempool_ws_connected = Gauge(
    "mevbot_mempool_ws_connected",
    "WebSocket connection state (0/1) per endpoint",
    ["endpoint"]
)

# Aggregated / deduped metrics
mempool_unique_tx_total = Counter(
    "mevbot_mempool_unique_tx_total",
    "Total unique pending tx observed (dedup across endpoints)"
)
mempool_tps = Gauge(
    "mevbot_mempool_tps",
    "Mempool transactions per second (rolling 60s)"
)
mempool_tpm = Gauge(
    "mevbot_mempool_tpm",
    "Mempool transactions per minute (rolling 60s)"
)

# Latency/processing (optional, filled if you add timings)
mempool_message_latency_ms = Histogram(
    "mevbot_mempool_message_latency_ms",
    "Time from WS recv -> enqueue/process",
    buckets=[1, 2, 5, 10, 20, 50, 100, 250, 500, 1000]
)

mempool_stream_publish_total = Counter(
    "mevbot_mempool_stream_publish_total",
    "Total tx hashes published to Redis stream",
    ["stream"]
)
mempool_stream_publish_errors_total = Counter(
    "mevbot_mempool_stream_publish_errors_total",
    "Total Redis publish errors",
    ["stream"]
)

mempool_stream_consume_total = Counter(
    "mevbot_mempool_stream_consume_total",
    "Entries consumed from Redis stream",
    ["stream"]
)
mempool_stream_consume_lag_ms = Histogram(
    "mevbot_mempool_stream_consume_lag_ms",
    "Lag between published ts and now (ms)",
    buckets=[10, 50, 100, 250, 500, 1000, 2000, 5000, 10000]
)
rpc_gettx_ok_total = Counter(
    "mevbot_rpc_gettx_ok_total",
    "Successful eth_getTransactionByHash calls"
)
rpc_gettx_errors_total = Counter(
    "mevbot_rpc_gettx_errors_total",
    "Failed eth_getTransactionByHash calls"
)
dex_tx_detected_total = Counter(
    "mevbot_dex_tx_detected_total",
    "Transactions that look like DEX swaps"
)

# --- Orderflow metrics ---
from prometheus_client import Counter, Gauge, Histogram

orderflow_submit_total = Counter(
    "mevbot_orderflow_submit_total",
    "Total JSON-RPC submissions to private endpoints",
    ["endpoint", "method"]
)
orderflow_submit_success_total = Counter(
    "mevbot_orderflow_submit_success_total",
    "Successful submissions by endpoint/method",
    ["endpoint", "method"]
)
orderflow_submit_fail_total = Counter(
    "mevbot_orderflow_submit_fail_total",
    "Failed submissions by endpoint/method/kind",
    ["endpoint", "method", "kind"]  # kind: rpc_error|transport
)
orderflow_submit_latency_ms = Histogram(
    "mevbot_orderflow_submit_latency_ms",
    "Submission latency in ms by endpoint/method",
    ["endpoint", "method"],
    buckets=[5,10,20,50,100,200,500,1000,2000,5000]
)
orderflow_endpoint_healthy = Gauge(
    "mevbot_orderflow_endpoint_healthy",
    "Endpoint health (1=recent success, 0=recent failure)",
    ["endpoint"]
)
sim_single_total = Counter("mevbot_sim_single_total", "Total single-tx simulations attempted")
sim_single_success_total = Counter("mevbot_sim_single_success_total", "Successful single-tx sims")
sim_single_fail_total = Counter("mevbot_sim_single_fail_total", "Failed single-tx sims", ["kind"])  # revert|error|policy

sim_bundle_total = Counter("mevbot_sim_bundle_total", "Total bundle simulations attempted")
sim_bundle_success_total = Counter("mevbot_sim_bundle_success_total", "Successful bundle sims")
sim_bundle_fail_total = Counter("mevbot_sim_bundle_fail_total", "Failed bundle sims", ["kind"])  # no_endpoint|revert|exhausted

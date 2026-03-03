# bot/telemetry/metrics.py
import os
from prometheus_client import Counter, Gauge, Histogram
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess
from starlette.responses import Response
from bot.core.telemetry import ensure_prometheus_multiproc_ready
CHAIN = os.getenv("CHAIN", "polygon")

WS_CONNECTIONS = Gauge("ws_connections", "Active WS mempool connections")

# NOTE: Counter("mempool_pending_tx", ...) exports as "mempool_pending_tx_total"
PENDING_TX_TOTAL      = Counter("mempool_pending_tx", "Pending tx observed", ["chain"])
HUNTER_FLAGGED_TOTAL  = Counter("hunter_flagged",     "Snipers flagged",     ["chain"])
BACKRUN_SUBMIT_TOTAL  = Counter("backrun_submit",     "Backrun submissions", ["chain"])
BACKRUN_SUCCESS_TOTAL = Counter("backrun_success",    "Successful backruns", ["chain"])

EXACT_OUTPUT_BUILD_TOTAL  = Counter("exact_output_build_total",  "Built exactOutputSingle calldata", ["chain"])
EXACT_OUTPUT_SIM_PASS     = Counter("exact_output_sim_pass_total","Simulation passed", ["chain"])
EXACT_OUTPUT_SIM_REVERT   = Counter("exact_output_sim_revert_total","Simulation reverted", ["chain"])

PRIVATE_SUBMIT_ATTEMPTS = Counter(
    "private_submit_attempts_total", "Private orderflow submit attempts", ["endpoint"]
)
PRIVATE_SUBMIT_SUCCESS = Counter(
    "private_submit_success_total", "Private orderflow submit successes", ["endpoint"]
)
PRIVATE_SUBMIT_ERRORS = Counter(
    "private_submit_errors_total", "Private orderflow submit errors", ["endpoint", "kind"]
)  

DETECT_LATENCY_MS = Histogram(
    "detect_latency_ms", "Detection->execution latency (ms)",
    buckets=[1,2,5,10,20,50,100,200,500,1000,2000,5000]
)

def warm_metrics() -> None:
    # Make labeled series exist at 0 so Grafana/Prom show them even before first event
    PENDING_TX_TOTAL.labels(chain=CHAIN).inc(0)
    HUNTER_FLAGGED_TOTAL.labels(chain=CHAIN).inc(0)
    BACKRUN_SUBMIT_TOTAL.labels(chain=CHAIN).inc(0)
    BACKRUN_SUCCESS_TOTAL.labels(chain=CHAIN).inc(0)
    WS_CONNECTIONS.set(0)

def mount_metrics(app, route: str = "/metrics"):
    from prometheus_client import make_asgi_app
    app.mount(path, make_asgi_app())


def _registry_for_this_process():
    if ensure_prometheus_multiproc_ready():
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        return reg
    from prometheus_client import REGISTRY
    return REGISTRY

async def metrics_endpoint(_req):
    return Response(generate_latest(_registry_for_this_process()),
                    media_type=CONTENT_TYPE_LATEST)

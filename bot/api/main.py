# bot/api/main.py
import os, time, asyncio, logging
from typing import Optional, List

from fastapi import FastAPI, APIRouter
from prometheus_client import make_asgi_app
from bot.telemetry.metrics import warm_metrics  # registers metrics
from web3 import Web3, HTTPProvider

from bot.mempool.monitor import WSMempoolMonitor
from bot.telemetry.alerts import AlertManager, AlertCfg
import bot.telemetry.metrics as metrics
from bot.telemetry.metrics import (
    CHAIN,
    PENDING_TX_TOTAL,
    DETECT_LATENCY_MS,
    PRIVATE_SUBMIT_ATTEMPTS, 
    PRIVATE_SUBMIT_SUCCESS, 
    PRIVATE_SUBMIT_ERRORS
)

# ---- App & /metrics ---------------------------------------------------------
app = FastAPI(title="MEV Bot API")
app.mount("/metrics", make_asgi_app())   # no 307 needed if Prometheus uses /metrics/

# single global so health can see it
_monitor: Optional[WSMempoolMonitor] = None

router = APIRouter()

@router.post("/debug/private-submit/ok")
def debug_private_submit_ok(endpoint: str = "demo"):
    PRIVATE_SUBMIT_ATTEMPTS.labels(endpoint=endpoint).inc()
    PRIVATE_SUBMIT_SUCCESS.labels(endpoint=endpoint).inc()
    return {"ok": True, "endpoint": endpoint}

@router.post("/debug/private-submit/fail")
def debug_private_submit_fail(endpoint: str = "demo", kind: str = "http_500"):
    PRIVATE_SUBMIT_ATTEMPTS.labels(endpoint=endpoint).inc()
    PRIVATE_SUBMIT_ERRORS.labels(endpoint=endpoint, kind=kind).inc()
    return {"ok": False, "endpoint": endpoint, "kind": kind}

# ---- Debug endpoint to materialize series quickly ---------------------------
@app.post("/debug/bump")
def bump():
    from bot.telemetry.metrics import PENDING_TX_TOTAL, CHAIN
    PENDING_TX_TOTAL.labels(chain=CHAIN).inc()
    return {"ok": True}


@app.on_event("startup")
async def _warm_prom():
    metrics.warm_metrics()

# ---- Startup / Shutdown -----------------------------------------------------
def _ws_env_endpoints() -> List[str]:
    vals = [
        os.getenv("WS_POLYGON_1", "").strip(),
        os.getenv("WS_POLYGON_2", "").strip(),
        os.getenv("WS_POLYGON_3", "").strip(),
    ]
    return [v for v in vals if v]

@app.on_event("startup")
async def _startup():
    logging.basicConfig(level=logging.INFO)

    # Alerts (optional)
    app.state.alerts = AlertManager(AlertCfg(
        webhook=os.getenv("DISCORD_WEBHOOK", ""),
        service=os.getenv("SERVICE_NAME", "mev-bot"),
        enabled=os.getenv("ALERTS_ENABLED", "true").lower() == "true",
        default_cooldown_s=int(os.getenv("ALERTS_DEFAULT_COOLDOWN", "60")),
    ))

    # Web3 (optional)
    rpc = os.getenv("RPC_ENDPOINT_PRIMARY", "").strip()
    app.state.w3 = Web3(HTTPProvider(rpc)) if rpc else None
    if app.state.w3 and not app.state.w3.is_connected():
        logging.warning("[startup] cannot connect to RPC %s", rpc)

    # Seed metrics so Prom/Graf see series immediately
    try:
        # define inline seeding to avoid import errors if not present in metrics.py
        PENDING_TX_TOTAL.labels(chain=CHAIN).inc(0)
        # If you have these counters in metrics.py, uncomment to seed them too:
        # HUNTER_FLAGGED_TOTAL.labels(chain=CHAIN).inc(0)
        # BACKRUN_SUBMIT_TOTAL.labels(chain=CHAIN).inc(0)
        # BACKRUN_SUCCESS_TOTAL.labels(chain=CHAIN).inc(0)
    except Exception:
        pass

    # Start mempool monitor if any WS endpoints are set
    endpoints = _ws_env_endpoints()
    global _monitor
    if endpoints:
        _monitor = WSMempoolMonitor(
            endpoints=endpoints,
            metrics_port=None,
            redis_stream=os.getenv("REDIS_STREAM", "mempool:pending:txs"),
            redis_url=os.getenv("REDIS_URL", "redis://mev-redis:6379/0"),
        )
        asyncio.create_task(_monitor.start())
        logging.info("WSMempoolMonitor starting with %d endpoints: %s", len(endpoints), endpoints)
    else:
        logging.warning("No WS_POLYGON_* endpoints set; mempool monitor not started.")
   
async def _warm():
    warm_metrics()


@app.on_event("shutdown")
async def _shutdown():
    if _monitor:
        await _monitor.stop()
    if getattr(app.state, "alerts", None):
        await app.state.alerts.close()


# ---- Health -----------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "time": int(time.time()),
        "w3_connected": bool(getattr(app.state, "w3", None) and app.state.w3.is_connected()),
        # treat monitor as present if we created it; some classes don’t expose .running
        "mempool_monitor": bool(_monitor),
        "endpoints": _ws_env_endpoints(),
    }


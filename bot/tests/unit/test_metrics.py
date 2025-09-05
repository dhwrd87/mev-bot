from fastapi import FastAPI
from starlette.testclient import TestClient
from bot.telemetry.metrics import mount_metrics, PENDING_TX_TOTAL

def test_metrics_endpoint_exposes_counters():
    app = FastAPI(); mount_metrics(app)
    PENDING_TX_TOTAL.labels(chain="polygon").inc()
    c = TestClient(app)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert b"mempool_pending_tx_total" in r.content

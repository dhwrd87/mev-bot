from fastapi import FastAPI
from starlette.testclient import TestClient
from bot.core.telemetry import mount_metrics, PENDING_TX_TOTAL

def test_metrics_endpoint_exposes_counters():
    app = FastAPI(); mount_metrics(app)
    PENDING_TX_TOTAL.labels(family="evm", chain="polygon", network="mainnet").inc()
    c = TestClient(app)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert b"mempool_pending_tx_total" in r.content
    assert b"bot_state" in r.content
    assert b"trades_sent_total" in r.content
    assert b"trades_failed_total" in r.content
    assert b"rpc_latency_seconds" in r.content

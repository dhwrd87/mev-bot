import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import bot.api.main as api_main


def _db_ready() -> bool:
    try:
        import psycopg

        dsn = os.getenv("DATABASE_URL") or (
            f"postgresql://{os.getenv('POSTGRES_USER','mevbot')}:{os.getenv('POSTGRES_PASSWORD','mevbot_pw')}"
            f"@{os.getenv('POSTGRES_HOST','127.0.0.1')}:{os.getenv('POSTGRES_PORT','5432')}"
            f"/{os.getenv('POSTGRES_DB','mevbot')}"
        )
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.integration
def test_pause_resume_persist_across_restart(monkeypatch):
    if not _db_ready():
        pytest.skip("postgres not available for pause/resume integration test")

    monkeypatch.setattr(api_main, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(api_main, "missing_required_env", lambda: [])
    monkeypatch.setenv("WS_POLYGON_1", "")
    monkeypatch.setenv("WS_POLYGON_2", "")
    monkeypatch.setenv("WS_POLYGON_3", "")

    # deterministic local DB defaults for host-side test execution
    monkeypatch.setenv("POSTGRES_HOST", os.getenv("POSTGRES_HOST", "127.0.0.1"))
    monkeypatch.setenv("POSTGRES_PORT", os.getenv("POSTGRES_PORT", "5432"))
    monkeypatch.setenv("POSTGRES_USER", os.getenv("POSTGRES_USER", "mevbot"))
    monkeypatch.setenv("POSTGRES_PASSWORD", os.getenv("POSTGRES_PASSWORD", "mevbot_pw"))
    monkeypatch.setenv("POSTGRES_DB", os.getenv("POSTGRES_DB", "mevbot"))

    with TestClient(api_main.app) as client:
        # normalize to unpaused baseline
        res = client.post("/resume")
        assert res.status_code == 200
        assert res.json()["paused"] is False

        res = client.post("/pause")
        assert res.status_code == 200
        assert res.json()["paused"] is True

    # new lifecycle should read paused=true from ops_state
    with TestClient(api_main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["paused"] is True

        res = client.post("/resume")
        assert res.status_code == 200
        assert res.json()["paused"] is False

    # second restart should retain resumed state
    with TestClient(api_main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["paused"] is False

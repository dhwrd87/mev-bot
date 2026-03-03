from __future__ import annotations

from pathlib import Path

from bot.core import telemetry


def test_multiproc_corrupt_dir_recovered(monkeypatch, tmp_path):
    mp = tmp_path / "prom_mp"
    mp.mkdir(parents=True, exist_ok=True)
    (mp / "counter_1.db").write_bytes(b"\xff\xfe\x00corrupt")

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(mp))
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_REQUIRED", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")

    import prometheus_client.multiprocess as mp_mod

    def _boom(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(mp_mod, "MultiProcessCollector", _boom)
    telemetry.ensure_prometheus_multiproc_ready(force=True)

    assert mp.exists()
    assert list(mp.iterdir()) == []


def test_multiproc_disabled_for_single_worker(monkeypatch, tmp_path):
    mp = tmp_path / "prom_mp2"
    mp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(mp))
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_REQUIRED", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")

    enabled = telemetry.ensure_prometheus_multiproc_ready(force=True)
    assert enabled is False
    assert "PROMETHEUS_MULTIPROC_DIR" not in __import__("os").environ


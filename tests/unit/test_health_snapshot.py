from __future__ import annotations

import json

from ops.health_snapshot import HealthSnapshotWriter


def test_health_snapshot_writer_writes_required_fields(tmp_path):
    out = tmp_path / "health_snapshot.json"
    w = HealthSnapshotWriter(path=str(out), interval_s=10.0)

    wrote = w.maybe_write(
        family="evm",
        chain="sepolia",
        state="PAUSED",
        mode="paper",
        now=1_700_000_000.0,
    )
    assert wrote is True
    assert out.exists()
    assert not (tmp_path / ".health_snapshot.json.tmp").exists()

    payload = json.loads(out.read_text(encoding="utf-8"))
    required = {
        "ts",
        "family",
        "chain",
        "network",
        "state",
        "mode",
        "head",
        "slot",
        "lag",
        "last_trade_ts",
        "tx_sent_10m",
        "tx_failed_10m",
        "trades_sent_10m",
        "trades_failed_10m",
        "rpc_p95_ms",
        "rpc_p99_ms",
        "rpc_errors_10m",
        "opportunities_seen_10m",
        "opportunities_attempted_10m",
        "opportunities_filled_10m",
        "confirm_p95_ms",
        "pnl_today_usd",
        "drawdown_usd",
        "fees_today_usd",
        "dex_health_summary",
    }
    assert required.issubset(set(payload.keys()))


def test_health_snapshot_writer_respects_interval(tmp_path):
    out = tmp_path / "health_snapshot.json"
    w = HealthSnapshotWriter(path=str(out), interval_s=10.0)

    assert w.maybe_write(family="evm", chain="sepolia", state="PAUSED", mode="paper", now=1000.0) is True
    assert w.maybe_write(family="evm", chain="sepolia", state="PAUSED", mode="paper", now=1005.0) is False
    assert w.maybe_write(family="evm", chain="sepolia", state="PAUSED", mode="paper", now=1010.1) is True

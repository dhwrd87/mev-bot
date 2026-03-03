from __future__ import annotations

import json
from pathlib import Path

from core.harness import run_paper_harness
from ops import metrics as ops_metrics


def _metric_sum(metric, match: dict[str, str]) -> float:
    total = 0.0
    labelnames = tuple(getattr(metric, "_labelnames", ()) or ())
    for labels, child in (getattr(metric, "_metrics", {}) or {}).items():
        lbl = {labelnames[i]: str(labels[i]) for i in range(min(len(labelnames), len(labels)))}
        if any(lbl.get(k) != v for k, v in match.items()):
            continue
        total += float(child._value.get())
    return float(total)


def test_harness_paper_mode_updates_metrics(tmp_path: Path, monkeypatch):
    op_path = tmp_path / "operator_state.json"
    snap_path = tmp_path / "health_snapshot.json"
    monkeypatch.setenv("CHAIN", "sepolia")
    monkeypatch.setenv("CHAIN_FAMILY", "evm")
    monkeypatch.setenv("CHAIN_NETWORK", "testnet")

    base = {"family": "evm", "chain": "sepolia", "network": "testnet"}
    seen_before = _metric_sum(ops_metrics.opportunities_seen_total, base)
    attempted_before = _metric_sum(ops_metrics.opportunities_attempted_total, base)
    executed_before = _metric_sum(ops_metrics.opportunities_executed_total, base)
    pnl_before = _metric_sum(ops_metrics.pnl_realized_usd, {**base, "strategy": "harness"})

    summary = run_paper_harness(
        duration_s=1.0,
        tick_s=0.05,
        operator_state_path=str(op_path),
        snapshot_path=str(snap_path),
        sim_pattern="ok,fail,ok",
    )
    assert summary["processed"] > 0
    assert snap_path.exists()

    seen_after = _metric_sum(ops_metrics.opportunities_seen_total, base)
    attempted_after = _metric_sum(ops_metrics.opportunities_attempted_total, base)
    executed_after = _metric_sum(ops_metrics.opportunities_executed_total, base)
    pnl_after = _metric_sum(ops_metrics.pnl_realized_usd, {**base, "strategy": "harness"})

    assert seen_after > seen_before
    assert attempted_after >= attempted_before
    assert executed_after >= executed_before
    assert pnl_after >= pnl_before

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "opportunities_seen_10m" in snap
    assert "opportunities_attempted_10m" in snap
    assert "opportunities_executed_10m" in snap
    assert "top_reject_reasons_10m" in snap

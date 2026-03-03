#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.harness import run_paper_harness


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run synthetic opportunity harness in paper mode.")
    p.add_argument("--duration", type=float, default=15.0, help="How long to run the harness loop.")
    p.add_argument("--tick", type=float, default=0.25, help="Tick interval in seconds.")
    p.add_argument(
        "--sim-pattern",
        default="ok,ok,ok,fail",
        help="Comma-separated simulation outcomes (ok/fail). Pattern repeats.",
    )
    p.add_argument("--snapshot-path", default="ops/health_snapshot.json", help="Health snapshot output path.")
    p.add_argument(
        "--operator-state-path",
        default="runtime/harness_operator_state.json",
        help="Operator state file path used by orchestrator gate.",
    )
    p.add_argument("--summary-out", default="", help="Optional output JSON file path.")
    return p.parse_args()


def main() -> int:
    ns = _args()
    summary = run_paper_harness(
        duration_s=float(ns.duration),
        tick_s=float(ns.tick),
        sim_pattern=str(ns.sim_pattern),
        snapshot_path=str(ns.snapshot_path),
        operator_state_path=str(ns.operator_state_path),
    )
    out = json.dumps(summary, indent=2, sort_keys=True)
    print(out)
    if ns.summary_out:
        p = Path(str(ns.summary_out))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(out + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

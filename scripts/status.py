#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Probe:
    path: str
    pattern: Optional[str] = None
    required: bool = True


@dataclass
class Item:
    category: str
    name: str
    probes: Sequence[Probe]
    next_change: str
    board_task: Optional[str] = None


def _exists(path: Path) -> bool:
    return path.exists()


def _first_match_line(path: Path, pattern: str) -> Optional[int]:
    if not path.exists() or not path.is_file():
        return None
    rx = re.compile(pattern)
    for i, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
        if rx.search(line):
            return i
    return None


def _probe_result(probe: Probe) -> tuple[bool, Optional[str]]:
    p = ROOT / probe.path
    if probe.pattern is None:
        ok = _exists(p)
        return ok, f"{probe.path}" if ok else None
    line = _first_match_line(p, probe.pattern)
    if line is not None:
        return True, f"{probe.path}:{line}"
    return False, None


def _state(results: List[bool], board_column: Optional[str]) -> str:
    col = (board_column or "").strip().lower()
    if col == "backlog":
        return "TODO"
    if col in {"ready", "in progress"}:
        return "PARTIAL" if any(results) else "TODO"
    if results and all(results):
        return "DONE"
    if any(results):
        return "PARTIAL"
    return "TODO"


def _load_build_board_columns() -> Dict[str, str]:
    board = ROOT / "docs" / "BUILD_BOARD.md"
    if not board.exists():
        return {}
    cols: Dict[str, str] = {}
    for raw in board.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 2:
            continue
        task, column = parts[0], parts[1]
        if task in {"Task", "---"} or column in {"Column", "---"}:
            continue
        cols[task] = column
    return cols


def _architecture_summary() -> List[str]:
    compose = ROOT / "docker" / "docker-compose.yml"
    services: List[str] = []
    in_services = False
    if compose.exists():
        for line in compose.read_text(errors="ignore").splitlines():
            if line.strip() == "services:":
                in_services = True
                continue
            if in_services:
                if re.match(r"^[^\s]", line):
                    break
                m = re.match(r"^\s{2}([a-zA-Z0-9_-]+):\s*$", line)
                if m:
                    services.append(m.group(1))

    modules = [
        "bot/mempool",
        "bot/workers",
        "bot/exec",
        "bot/orchestration",
        "bot/storage",
        "bot/telemetry",
    ]
    module_present = [m for m in modules if (ROOT / m).exists()]

    out = []
    out.append("Services: " + (", ".join(services) if services else "unknown"))
    out.append("Core modules: " + (", ".join(module_present) if module_present else "unknown"))
    out.append("Primary docs: docs/BUILD_BOARD.md, docs/TEST_MAP.md, docs/WIRING_MAP.md")
    return out


def _items() -> List[Item]:
    return [
        Item(
            category="Platform",
            name="Compose stack and health checks",
            probes=[
                Probe("docker/docker-compose.yml", r"mev-bot:"),
                Probe("docker/docker-compose.yml", r"healthcheck:"),
                Probe("docker/docker-compose.yml", r"postgres:"),
            ],
            next_change="Add a single `make up-validate` target that runs compose up and smoke in sequence.",
            board_task="Bootstrap repo & compose",
        ),
        Item(
            category="Platform",
            name="Migration framework and linked SQL migrations",
            probes=[
                Probe("scripts/migrate.py"),
                Probe("migrations/0102_mempool_pipeline_persistence.sql"),
                Probe("migrations/0103_candidates_paper_mode.sql"),
                Probe("migrations/0104_candidates_outcomes.sql"),
            ],
            next_change="Add `make migrate` alias to standardize migration invocation for operators.",
            board_task="Postgres + migrations",
        ),
        Item(
            category="Platform",
            name="Status report generation from script",
            probes=[
                Probe("scripts/status.py"),
                Probe("Makefile", r"^status:"),
            ],
            next_change="Wire STATUS generation into CI so docs/status drift is caught automatically.",
            board_task="Runtime status command (`scripts/status.py` + `make status`)",
        ),
        Item(
            category="Mempool Pipeline",
            name="WS producer wired to redis stream",
            probes=[
                Probe("docker/docker-compose.yml", r"mempool-producer:"),
                Probe("docker/docker-compose.yml", r"python3 -m bot\.mempool\.ws_subscribe"),
                Probe("bot/mempool/ws_subscribe.py", r"xadd"),
            ],
            next_change="Add producer integration test that asserts stream writes from a mocked websocket.",
            board_task="WS->Redis publisher path + consumer handoff wiring",
        ),
        Item(
            category="Mempool Pipeline",
            name="Consumer persists stream events and tx/error tables",
            probes=[
                Probe("bot/workers/mempool_consumer.py", r"insert_mempool_event"),
                Probe("bot/workers/mempool_consumer.py", r"upsert_mempool_tx"),
                Probe("bot/workers/mempool_consumer.py", r"insert_mempool_error"),
            ],
            next_change="Add a unit test with fake Redis + fake RPC covering event/tx/error persistence branches.",
            board_task="Mempool monitor (multi-WS)",
        ),
        Item(
            category="Mempool Pipeline",
            name="DB debug stats endpoint for pipeline tables",
            probes=[
                Probe("bot/api/main.py", r"@app\.get\(\"/debug/db_stats\"\)"),
                Probe("bot/api/main.py", r"mempool_events"),
            ],
            next_change="Expose DB stats in Prometheus metrics to enable alert thresholds.",
            board_task="Telemetry baseline",
        ),
        Item(
            category="Detection",
            name="Paper candidate detector worker",
            probes=[
                Probe("bot/workers/candidate_detector.py"),
                Probe("bot/workers/candidate_detector.py", r"high_priority_fee"),
                Probe("bot/workers/candidate_detector.py", r"allowlist_hit"),
            ],
            next_change="Add small deterministic fixture test for allowlist and priority-fee candidate emissions.",
        ),
        Item(
            category="Detection",
            name="Allowlist config file present",
            probes=[Probe("config/allowlist.json")],
            next_change="Populate non-empty allowlist per chain and validate addresses at load time.",
        ),
        Item(
            category="Detection",
            name="Candidates API endpoint",
            probes=[
                Probe("bot/api/main.py", r"@app\.get\(\"/candidates\"\)"),
                Probe("bot/api/main.py", r"FROM candidates"),
            ],
            next_change="Add query params (`kind`, `limit`, `since`) to support targeted review workflows.",
        ),
        Item(
            category="Simulation",
            name="Simulator smoke path",
            probes=[
                Probe("scripts/sim_smoke.py"),
                Probe("Makefile", r"^sim-smoke:"),
            ],
            next_change="Add a CI job that runs `make sim-smoke` on every PR.",
            board_task="Pre-submit simulation",
        ),
        Item(
            category="Simulation",
            name="Pre-submit simulation integration",
            probes=[
                Probe("bot/sim/pre_submit.py"),
                Probe("docs/BUILD_BOARD.md", r"Pre-submit simulation"),
            ],
            next_change="Hook pre-submit simulation into all execution paths and mark board item DONE with proof.",
            board_task="Pre-submit simulation",
        ),
        Item(
            category="Execution",
            name="Private orderflow router module",
            probes=[
                Probe("bot/exec/orderflow.py"),
                Probe("bot/exec/orderflow.py", r"orderflow_endpoint_healthy"),
            ],
            next_change="Add regression tests for timeout + fallback ordering between relay endpoints.",
            board_task="Private orderflow router",
        ),
        Item(
            category="Execution",
            name="Stealth execution flow",
            probes=[
                Probe("bot/strategy/stealth.py"),
                Probe("scripts/stealth_e2e.py"),
            ],
            next_change="Automate `stealth_e2e` artifact capture in CI nightly runs.",
            board_task="Stealth E2E",
        ),
        Item(
            category="Execution",
            name="Hunter execution flow",
            probes=[
                Probe("bot/hunter/runner.py"),
                Probe("scripts/hunter_e2e.py"),
                Probe("docs/BUILD_BOARD.md", r"Hunter E2E \| Ready"),
            ],
            next_change="Produce one reproducible proof artifact from `scripts/hunter_e2e.py` and move board row to Done.",
            board_task="Hunter E2E",
        ),
        Item(
            category="PnL/Accounting",
            name="Trades and daily pnl schema",
            probes=[
                Probe("sql/migrations/0001_init.sql", r"CREATE TABLE IF NOT EXISTS trades"),
                Probe("sql/migrations/0001_init.sql", r"CREATE TABLE IF NOT EXISTS pnl_daily"),
            ],
            next_change="Add write path from execution results to `pnl_daily` rollups.",
            board_task="Postgres + migrations",
        ),
        Item(
            category="PnL/Accounting",
            name="Nightly ETL/report scripts",
            probes=[
                Probe("scripts/nightly_etl.py"),
                Probe("scripts/weekly_report.py"),
                Probe("docs/BUILD_BOARD.md", r"Nightly ETL -> DuckDB \| Backlog"),
            ],
            next_change="Add one cron-compatible entrypoint and proof artifact path for nightly ETL outputs.",
            board_task="Nightly ETL -> DuckDB",
        ),
        Item(
            category="Observability",
            name="Prometheus + Grafana stack",
            probes=[
                Probe("docker/docker-compose.yml", r"prometheus:"),
                Probe("docker/docker-compose.yml", r"grafana:"),
                Probe("bot/core/telemetry.py"),
            ],
            next_change="Add dashboard coverage for paper-mode candidate and outcome metrics.",
            board_task="Grafana dashboards",
        ),
        Item(
            category="Observability",
            name="Paper report endpoint",
            probes=[
                Probe("bot/api/main.py", r"@app\.get\(\"/paper_report\"\)"),
                Probe("bot/api/main.py", r"success_rate"),
            ],
            next_change="Add p50/p95 inclusion delay and outcome counts by candidate kind.",
        ),
        Item(
            category="Observability",
            name="Smoke coverage for pipeline and paper endpoints",
            probes=[
                Probe("smoke.sh", r"DB persistence checks"),
                Probe("smoke.sh", r"paper_report_http"),
            ],
            next_change="Add smoke checks for evaluator worker process health and non-zero evaluated outcomes.",
        ),
        Item(
            category="Safety/Risk",
            name="Pause/resume kill switch API",
            probes=[
                Probe("bot/api/main.py", r"@app\.post\(\"/pause\"\)"),
                Probe("bot/api/main.py", r"@app\.post\(\"/resume\"\)"),
                Probe("tests/integration/test_pause_api.py"),
            ],
            next_change="Add authorization layer for pause/resume endpoints.",
            board_task="Kill-switch & API",
        ),
        Item(
            category="Safety/Risk",
            name="Adaptive risk manager module",
            probes=[
                Probe("bot/risk/adaptive.py"),
                Probe("docs/BUILD_BOARD.md", r"AdaptiveRiskManager \| In Progress"),
            ],
            next_change="Implement and test hard-cap enforcement path, then close remaining board acceptance criteria.",
            board_task="AdaptiveRiskManager",
        ),
        Item(
            category="Safety/Risk",
            name="Secrets hardening / access control",
            probes=[
                Probe("scripts/secret_scan.py"),
                Probe("bot/security"),
                Probe("docs/BUILD_BOARD.md", r"Secrets hardening \| Backlog"),
                Probe("docs/BUILD_BOARD.md", r"Access control \| Backlog"),
            ],
            next_change="Add API key middleware and deny-by-default allowlist check with one integration test.",
            board_task="Access control",
        ),
        Item(
            category="Detection",
            name="Paper evaluator outcomes worker",
            probes=[
                Probe("bot/workers/candidate_evaluator.py"),
                Probe("bot/workers/candidate_evaluator.py", r"eth_getTransactionReceipt"),
                Probe("bot/workers/candidate_evaluator.py", r"EVAL_TIMEOUT_S"),
            ],
            next_change="Run evaluator as a dedicated compose service and expose its health/status metric.",
        ),
    ]


def generate_status_markdown() -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    items = _items()
    board_cols = _load_build_board_columns()

    cats: List[str] = [
        "Platform",
        "Mempool Pipeline",
        "Detection",
        "Simulation",
        "Execution",
        "PnL/Accounting",
        "Observability",
        "Safety/Risk",
    ]

    lines: List[str] = []
    lines.append("# STATUS")
    lines.append("")
    lines.append(f"Generated: {now}")
    lines.append("")
    lines.append("## Architecture (Inferred)")
    for x in _architecture_summary():
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Sources")
    lines.append("- docs/BUILD_BOARD.md")
    lines.append("- docs/TEST_MAP.md")
    lines.append("- docs/WIRING_MAP.md")
    lines.append("- docs/ARCHITECTURE.md")
    lines.append("- RUNBOOK.md")
    lines.append("- docker/smoke.sh")
    if (ROOT / "scripts" / "smoke_all.py").exists():
        lines.append("- scripts/smoke_all.py")
    lines.append("")

    total = 0
    done = 0

    for cat in cats:
        lines.append(f"## {cat}")
        cat_items = [i for i in items if i.category == cat]
        for item in cat_items:
            total += 1
            bools: List[bool] = []
            evidence: List[str] = []
            for pr in item.probes:
                ok, ev = _probe_result(pr)
                bools.append(ok)
                if ev:
                    evidence.append(ev)
            state = _state(bools, board_cols.get(item.board_task or ""))
            if state == "DONE":
                done += 1

            lines.append(f"- **{item.name}**")
            lines.append(f"  - state: `{state}`")
            if item.board_task:
                lines.append(
                    f"  - board column: `{board_cols.get(item.board_task, 'unknown')}` ({item.board_task})"
                )
            lines.append(
                "  - evidence: "
                + (", ".join(f"`{e}`" for e in evidence) if evidence else "`none found`")
            )
            lines.append(f"  - next smallest change: {item.next_change}")
        lines.append("")

    lines.insert(4, f"Progress: {done}/{total} DONE")
    lines.insert(5, "")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate repo status report")
    ap.add_argument("--write", action="store_true", help="Write STATUS.md at repo root")
    ap.add_argument("--stdout", action="store_true", help="Print markdown to stdout")
    args = ap.parse_args()

    md = generate_status_markdown()

    did_any = False
    if args.write or not args.stdout:
        out = ROOT / "STATUS.md"
        out.write_text(md)
        print(f"Wrote {out}")
        did_any = True

    if args.stdout:
        print(md, end="")
        did_any = True

    return 0 if did_any else 1


if __name__ == "__main__":
    raise SystemExit(main())

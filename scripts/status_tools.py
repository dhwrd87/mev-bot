#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from typing import Dict, List, Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUILD_BOARD = os.path.join(ROOT, "docs", "BUILD_BOARD.md")
TEST_MAP = os.path.join(ROOT, "docs", "TEST_MAP.md")
WIRING_MAP = os.path.join(ROOT, "docs", "WIRING_MAP.md")
STATUS_JSON = os.path.join(ROOT, "STATUS.json")
STATUS_MD = os.path.join(ROOT, "STATUS.md")


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def parse_build_board(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    in_table = False
    rows: List[List[str]] = []
    for line in lines:
        if line.strip().startswith("| Task | Column |"):
            in_table = True
            continue
        if in_table:
            if line.strip().startswith("|---"):
                continue
            if not line.strip().startswith("|"):
                break
            parts = [p.strip() for p in line.strip().strip("|").split("|")]
            if len(parts) >= 5:
                rows.append(parts[:5])

    items = []
    for task, column, acceptance, proof, artifacts in rows:
        col = column.strip().lower()
        status = "todo"
        if col == "done":
            status = "done"
        elif col == "in progress":
            status = "doing"
        elif col == "ready" or col == "backlog":
            status = "todo"
        elif col == "blocked":
            status = "blocked"
        item = {
            "id": slugify(task),
            "title": task,
            "doc_ref": artifacts,
            "owner": "",
            "status": status,
            "acceptance": acceptance,
            "proof": proof,
            "evidence": [],
            "last_checked": None,
        }
        items.append(item)
    return items


def load_status(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_status() -> Dict[str, Any]:
    items = parse_build_board(BUILD_BOARD)
    existing = load_status(STATUS_JSON)
    by_id = {i["id"]: i for i in existing.get("items", [])}

    merged = []
    for item in items:
        prev = by_id.get(item["id"])
        if prev:
            item["owner"] = prev.get("owner", "")
            item["status"] = prev.get("status", item["status"])
            item["evidence"] = prev.get("evidence", [])
            item["last_checked"] = prev.get("last_checked")
        merged.append(item)

    status = {
        "generated_at": _now_iso(),
        "sources": {
            "build_board": "docs/BUILD_BOARD.md",
            "test_map": "docs/TEST_MAP.md",
            "wiring_map": "docs/WIRING_MAP.md",
        },
        "items": merged,
        "runtime_checks": existing.get("runtime_checks", {}),
    }
    return status


def render_status_md(status: Dict[str, Any]) -> str:
    items = status.get("items", [])
    done = len([i for i in items if i.get("status") == "done"])
    total = len(items)

    next_up = [i for i in items if i.get("status") != "done"][:3]

    lines = []
    lines.append("# STATUS")
    lines.append("")
    lines.append(f"Generated: {status.get('generated_at','')}")
    lines.append("")
    lines.append(f"Progress: {done}/{total} done")
    lines.append("")
    lines.append("## Next Up")
    for i in next_up:
        lines.append(f"- {i['title']} ({i['status']})")
    if not next_up:
        lines.append("- None")
    lines.append("")
    lines.append("## Items")
    lines.append("")
    lines.append("| Id | Title | Status | Owner | Last Checked | Doc Ref |")
    lines.append("|---|---|---|---|---|---|")
    for i in items:
        lines.append(
            f"| {i['id']} | {i['title']} | {i['status']} | {i.get('owner','')} | {i.get('last_checked','')} | {i.get('doc_ref','')} |"
        )
    lines.append("")
    lines.append("## Runtime Checks")
    lines.append("")
    rc = status.get("runtime_checks", {})
    for k, v in rc.items():
        lines.append(f"- {k}: {v.get('ok')} @ {v.get('ts')} ")
    lines.append("")
    return "\n".join(lines)


def write_status(status: Dict[str, Any]) -> None:
    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, sort_keys=False)
    with open(STATUS_MD, "w", encoding="utf-8") as f:
        f.write(render_status_md(status))


def snapshot(status: Dict[str, Any], checks: Dict[str, Any]) -> Dict[str, Any]:
    status["runtime_checks"] = checks
    status["generated_at"] = _now_iso()

    title_to_id = {i["title"]: i["id"] for i in status.get("items", [])}

    evidence_map = {
        "Bootstrap repo & compose": ["compose_smoke", "health"],
        "GAP-2: WS->Redis publisher runtime path": ["redis_xlen"],
        "GAP-3: consumer RPC env mismatch": ["consumer_fetch"],
        "WS->Redis publisher path + consumer handoff wiring": ["redis_xlen", "consumer_fetch"],
        "Telemetry baseline": ["health"],
        "Postgres + migrations": ["postgres"],
    }

    items_by_id = {i["id"]: i for i in status.get("items", [])}

    for title, check_keys in evidence_map.items():
        item_id = title_to_id.get(title)
        if not item_id:
            continue
        item = items_by_id[item_id]
        ev = item.get("evidence", [])
        for ck in check_keys:
            c = checks.get(ck)
            if c and c.get("ok"):
                ev_line = f"{ck} ok @ {c.get('ts')}"
                if ev_line not in ev:
                    ev.append(ev_line)
                item["last_checked"] = c.get("ts")
        item["evidence"] = ev

    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "render", "snapshot"]) 
    ap.add_argument("--checks", help="Path to checks JSON")
    args = ap.parse_args()

    if args.command == "build":
        status = build_status()
        write_status(status)
        return
    if args.command == "render":
        status = load_status(STATUS_JSON)
        write_status(status)
        return
    if args.command == "snapshot":
        status = build_status()
        checks = {}
        if args.checks:
            with open(args.checks, "r", encoding="utf-8") as f:
                checks = json.load(f)
        status = snapshot(status, checks)
        write_status(status)
        return

if __name__ == "__main__":
    main()

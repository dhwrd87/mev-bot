#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
API_BASE = "http://127.0.0.1:8000"
WAIT_SECONDS = 15
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker" / "docker-compose.yml"),
    "-f",
    str(ROOT / "docker" / "docker-compose.override.yml"),
    "--env-file",
    str(ROOT / ".env.runtime"),
]


def _http_json(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}{path}"
    try:
        with urlopen(url, timeout=5) as r:
            status = int(getattr(r, "status", 200))
            body = r.read().decode("utf-8", errors="ignore")
            return status, (json.loads(body) if body else {})
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        payload = {}
        if body:
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
        return int(e.code), payload
    except URLError as e:
        return 0, {"error": str(e)}


def _http_text(path: str) -> tuple[int, str]:
    url = f"{API_BASE}{path}"
    try:
        with urlopen(url, timeout=5) as r:
            return int(getattr(r, "status", 200)), r.read().decode("utf-8", errors="ignore")
    except HTTPError as e:
        return int(e.code), e.read().decode("utf-8", errors="ignore")
    except URLError as e:
        return 0, str(e)


def _psql_scalar(sql: str) -> int:
    cmd = COMPOSE + [
        "exec",
        "-T",
        "postgres",
        "sh",
        "-lc",
        (
            "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tA -c "
            + json.dumps(sql)
        ),
    ]
    out = subprocess.check_output(cmd, cwd=ROOT, text=True).strip()
    return int(out or "0")


def main() -> int:
    health_code, _ = _http_json("/health")
    if health_code != 200:
        print(f"FAIL: /health returned {health_code}")
        return 1

    before = _psql_scalar("select count(*) from candidates;")
    print(f"candidates_before={before}")
    print(f"waiting_seconds={WAIT_SECONDS}")
    time.sleep(WAIT_SECONDS)

    after = _psql_scalar("select count(*) from candidates;")
    print(f"candidates_after={after} delta={after - before}")
    if after <= before:
        print("FAIL: candidates inserted <= 0 during window")
        return 1

    decisions_code, decisions = _http_json("/debug/decisions")
    if decisions_code != 200:
        print(f"FAIL: /debug/decisions returned {decisions_code}")
        return 1
    total_decisions = int(decisions.get("total", 0))
    items = decisions.get("items", [])
    print(f"decisions_total={total_decisions} buckets={len(items)}")
    if total_decisions <= 0 or not isinstance(items, list) or len(items) == 0:
        print("FAIL: decisions missing")
        return 1

    cands_code, cands = _http_json("/debug/candidates?limit=50")
    if cands_code != 200:
        print(f"FAIL: /debug/candidates returned {cands_code}")
        return 1
    cand_items = cands.get("items", [])
    print(f"debug_candidates_items={len(cand_items)}")
    if not isinstance(cand_items, list) or len(cand_items) == 0:
        print("FAIL: /debug/candidates returned no rows")
        return 1

    metrics_code, metrics_text = _http_text("/metrics")
    if metrics_code != 200:
        print(f"FAIL: /metrics returned {metrics_code}")
        return 1
    required = [
        "mevbot_candidate_pipeline_seen_total",
        "mevbot_candidate_pipeline_detected_total",
        "mevbot_candidate_pipeline_decisions_total",
    ]
    missing = [m for m in required if m not in metrics_text]
    print("metrics_checked=" + ",".join(required))
    if missing:
        print("FAIL: missing candidate metrics: " + ",".join(missing))
        return 1

    print("PASS: golden path smoke checks succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

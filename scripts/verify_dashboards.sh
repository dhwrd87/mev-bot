#!/usr/bin/env bash
set -euo pipefail

PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
MAX_QUERIES="${MAX_QUERIES:-40}"

python3 - "$PROM_URL" "$MAX_QUERIES" <<'PY'
import glob
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

prom = sys.argv[1].rstrip("/")
max_queries = int(sys.argv[2])

base_dirs = [
    "grafana/dashboards/00-operator",
    "grafana/dashboards/10-execution",
    "grafana/dashboards/20-strategy",
    "grafana/dashboards/90-infra",
]

files = []
for d in base_dirs:
    files.extend(sorted(glob.glob(f"{d}/*.json")))

if not files:
    print("FAIL no provisioned dashboards found", file=sys.stderr)
    sys.exit(1)

required_vars = ["family", "chain", "network", "dex", "strategy", "provider"]

issues = []
queries = []

for f in files:
    path = Path(f)
    try:
        dash = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        issues.append(f"{f}: invalid json: {e}")
        continue

    vars_list = (dash.get("templating") or {}).get("list") or []
    vars_by_name = {str(v.get("name", "")): v for v in vars_list if isinstance(v, dict)}

    for name in required_vars:
        v = vars_by_name.get(name)
        if not v:
            issues.append(f"{f}: missing variable '{name}'")
            continue
        q = str(v.get("query", ""))
        expected = f"label_values(mevbot_heartbeat_ts, {name})"
        if q != expected:
            issues.append(f"{f}: variable '{name}' query must be exactly: {expected}")
        if not bool(v.get("includeAll", False)):
            issues.append(f"{f}: variable '{name}' includeAll must be true")
        if str(v.get("customAllValue", "")) != ".*":
            issues.append(f"{f}: variable '{name}' customAllValue must be '.*'")

    for panel in dash.get("panels", []) or []:
        panel_title = str(panel.get("title", "<untitled>"))
        for tgt in panel.get("targets", []) or []:
            expr = tgt.get("expr")
            if not isinstance(expr, str) or not expr.strip():
                continue
            queries.append((f, panel_title, expr.strip()))

if issues:
    print("DASHBOARD STRUCTURE FAILURES:")
    for i in issues:
        print(" -", i)
    sys.exit(1)

# de-duplicate while preserving order
seen = set()
uniq = []
for rec in queries:
    key = rec[2]
    if key in seen:
        continue
    seen.add(key)
    uniq.append(rec)

subset = uniq[:max_queries]
if not subset:
    print("FAIL no PromQL expressions found", file=sys.stderr)
    sys.exit(1)

print(f"Using Prometheus at: {prom}")
print(f"Checking {len(subset)} queries from {len(files)} dashboards (max={max_queries})")

parse_errors = []

for f, title, expr in subset:
    url = f"{prom}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        parse_errors.append((f, title, expr, f"request_error:{e}"))
        continue

    if payload.get("status") != "success":
        et = payload.get("errorType", "unknown")
        er = payload.get("error", "unknown")
        parse_errors.append((f, title, expr, f"{et}:{er}"))
        continue

    print(f"OK   {Path(f).name} :: {title}")

if parse_errors:
    print("\nPROMQL PARSE/API FAILURES:")
    for f, title, expr, err in parse_errors:
        print(f" - {Path(f).name} :: {title} :: {err}")
        print(f"   expr: {expr}")
    sys.exit(1)

print("Dashboard query verification passed.")
PY

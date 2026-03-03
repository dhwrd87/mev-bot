#!/usr/bin/env bash
set -euo pipefail

PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

require_cmd curl
require_cmd python3

TARGETS_JSON="$(curl -fsS "$PROM_URL/api/v1/targets")" || fail "cannot reach Prometheus targets API at $PROM_URL"

python3 - "$TARGETS_JSON" <<'PY' || exit 1
import json
import sys

payload = json.loads(sys.argv[1])
if payload.get("status") != "success":
    print("ERROR: targets API status is not success", file=sys.stderr)
    sys.exit(1)

active = payload.get("data", {}).get("activeTargets", [])
if not active:
    print("ERROR: no active targets returned", file=sys.stderr)
    sys.exit(1)

bot = [t for t in active if t.get("labels", {}).get("job") == "mev-bot"]
if not bot:
    print("ERROR: no mev-bot target found", file=sys.stderr)
    sys.exit(1)

bad = [t for t in bot if t.get("health") != "up"]
if bad:
    print("ERROR: mev-bot target is not UP", file=sys.stderr)
    for t in bad:
        print(f"  - {t.get('scrapeUrl')} health={t.get('health')} lastError={t.get('lastError')}", file=sys.stderr)
    sys.exit(1)

print("OK: mev-bot scrape target is UP")

optional_jobs = {"node-exporter", "cadvisor", "postgres-exporter", "redis-exporter"}
for job in sorted(optional_jobs):
    entries = [t for t in active if t.get("labels", {}).get("job") == job]
    if not entries:
        print(f"SKIP: optional job {job} not configured")
        continue
    down = [t for t in entries if t.get("health") != "up"]
    if down:
        print(f"ERROR: optional job {job} has non-UP targets", file=sys.stderr)
        for t in down:
            print(f"  - {t.get('scrapeUrl')} health={t.get('health')} lastError={t.get('lastError')}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: optional job {job} targets are UP")
PY

check_metric() {
  local metric="$1"
  local result
  result="$(curl -fsS -G "$PROM_URL/api/v1/query" --data-urlencode "query=$metric")" || fail "query failed for $metric"

  python3 - "$metric" "$result" <<'PY' || exit 1
import json
import sys

metric = sys.argv[1]
payload = json.loads(sys.argv[2])
if payload.get("status") != "success":
    print(f"ERROR: query failed for {metric}", file=sys.stderr)
    sys.exit(1)
res = payload.get("data", {}).get("result", [])
if not res:
    print(f"ERROR: metric {metric} has no series", file=sys.stderr)
    sys.exit(1)
print(f"OK: metric {metric} exists ({len(res)} series)")
PY
}

# Key bot metrics required by dashboards/alerts.
check_metric "mevbot_state"
check_metric "mevbot_tx_sent_total"
check_metric "mevbot_tx_failed_total"
check_metric "mevbot_rpc_errors_total"
check_metric "mevbot_opportunities_seen_total"

echo "All metric verification checks passed."

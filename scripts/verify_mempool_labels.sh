#!/usr/bin/env bash
set -euo pipefail

METRICS_URL="${METRICS_URL:-http://127.0.0.1:8000/metrics}"
PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

tmp_metrics="$(mktemp)"
trap 'rm -f "$tmp_metrics"' EXIT

echo "Checking metrics endpoint: ${METRICS_URL}"
curl -fsS "${METRICS_URL}" >"$tmp_metrics" || fail "cannot fetch ${METRICS_URL}"

python3 - "$tmp_metrics" <<'PY' || fail "mevbot_mempool_tps is missing canonical labels {family,chain,network}"
import re
import sys
path = sys.argv[1]
ok = False
for line in open(path, "r", encoding="utf-8", errors="ignore"):
    if not line.startswith("mevbot_mempool_tps{"):
        continue
    m = re.search(r"\{([^}]*)\}", line)
    if not m:
        continue
    labels = {}
    for part in m.group(1).split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        labels[k.strip()] = v.strip().strip('"')
    if labels.get("family") and labels.get("chain") and labels.get("network"):
        ok = True
        break
if not ok:
    raise SystemExit(1)
PY

echo "OK: mevbot_mempool_tps has canonical labels"

query='mevbot_mempool_tps{chain=~".+"}'
encoded_query="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlencode({"query": sys.argv[1]}))' "$query")"
prom_resp="$(curl -fsS "${PROM_URL}/api/v1/query?${encoded_query}")" || fail "cannot query Prometheus API at ${PROM_URL}"

python3 - "$prom_resp" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
if payload.get("status") != "success":
    raise SystemExit("Prometheus query failed: " + str(payload.get("error", "unknown")))
res = payload.get("data", {}).get("result", [])
if not res:
    raise SystemExit("empty result for mevbot_mempool_tps{chain=~\".+\"}")
print(f"OK: Prometheus returned {len(res)} series for mevbot_mempool_tps")
PY

echo "verify_mempool_labels.sh passed"

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

python3 - "$tmp_metrics" <<'PY' || fail "mempool metric labels validation failed"
import re
import sys

path = sys.argv[1]
required = [
    "mevbot_mempool_unique_tx_total",
    "mevbot_mempool_tps",
    "mevbot_mempool_tpm",
    "mevbot_mempool_stream_publish_total",
    "mevbot_mempool_stream_publish_errors_total",
    "mevbot_mempool_stream_consume_total",
    "mevbot_mempool_stream_consume_errors_total",
    "mevbot_mempool_consumer_throughput_tps",
    "mevbot_mempool_stream_xlen",
    "mevbot_mempool_stream_group_lag",
    "mevbot_mempool_dlq_writes_total",
    "mevbot_mempool_tps_legacy",
    "mevbot_mempool_tpm_legacy",
]
endpoint_required = [
    "mevbot_mempool_rx_total",
    "mevbot_mempool_rx_errors_total",
    "mevbot_mempool_reconnects_total",
    "mevbot_mempool_ws_connected",
    "mevbot_mempool_message_latency_ms",
    "mevbot_mempool_message_latency_ms_legacy",
    "mevbot_mempool_stream_consume_lag_ms",
    "mevbot_mempool_stream_consume_lag_ms_legacy",
]
required_set = set(required + endpoint_required)
found = {}

def parse_labels(s: str) -> dict[str, str]:
    m = re.search(r"\{([^}]*)\}", s)
    if not m:
        return {}
    out = {}
    for part in m.group(1).split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out

with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for raw in f:
        if raw.startswith("#"):
            continue
        metric = raw.split("{", 1)[0].split(" ", 1)[0].strip()
        if metric.endswith("_bucket") or metric.endswith("_sum") or metric.endswith("_count") or metric.endswith("_created"):
            metric = re.sub(r"_(bucket|sum|count|created)$", "", metric)
        if metric not in required_set:
            continue
        labels = parse_labels(raw)
        if not labels.get("family") or not labels.get("chain") or not labels.get("network"):
            raise SystemExit(f"{metric} missing family/chain/network labels")
        if metric in endpoint_required and not labels.get("endpoint"):
            raise SystemExit(f"{metric} missing endpoint label")
        found[metric] = True

missing = [m for m in required_set if m not in found]
if missing:
    raise SystemExit("missing metrics or unlabeled metrics: " + ", ".join(sorted(missing)))
print("OK: all checked mevbot_mempool_* metrics include required labels")
PY

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

query_ep='mevbot_mempool_rx_total{chain=~".+",endpoint=~".+"}'
encoded_query_ep="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlencode({"query": sys.argv[1]}))' "$query_ep")"
prom_resp_ep="$(curl -fsS "${PROM_URL}/api/v1/query?${encoded_query_ep}")" || fail "cannot query endpoint-labeled mempool metric via Prometheus API"

python3 - "$prom_resp_ep" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
if payload.get("status") != "success":
    raise SystemExit("Prometheus endpoint query failed: " + str(payload.get("error", "unknown")))
res = payload.get("data", {}).get("result", [])
if not res:
    raise SystemExit("empty result for mevbot_mempool_rx_total with endpoint label")
print(f"OK: Prometheus returned {len(res)} endpoint-labeled mempool series")
PY

echo "verify_mempool_labels.sh passed"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT_DIR/docker/docker-compose.yml" -f "$ROOT_DIR/docker/docker-compose.override.yml" --env-file "$ROOT_DIR/.env.runtime")
METRICS_URL="${METRICS_URL:-http://127.0.0.1:8000/metrics}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
TMP_DIR="${TMPDIR:-/tmp}/mev-verify-data-flow"
mkdir -p "$TMP_DIR"

fail() {
  local msg="$1"
  local next="${2:-Check docker compose logs for mev-bot, candidate-pipeline, mempool-consumer, mempool-producer.}"
  echo "FAIL: $msg" >&2
  echo "Next: $next" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

metric_sum() {
  local metric="$1"
  local file="$2"
  python3 - "$metric" "$file" <<'PY'
import sys
metric = sys.argv[1]
path = sys.argv[2]
s = 0.0
with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for ln in f:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith(metric + "{") or ln.startswith(metric + " "):
            try:
                s += float(ln.split()[-1])
            except Exception:
                pass
print(s)
PY
}

require_cmd curl
require_cmd python3
require_cmd docker

curl -fsS "$HEALTH_URL" >"$TMP_DIR/health.json" || fail "health endpoint unreachable at $HEALTH_URL"
CHAIN_FAMILY="$(python3 - "$TMP_DIR/health.json" <<'PY'
import json,sys
print((json.load(open(sys.argv[1])) or {}).get("chain_family","unknown"))
PY
)"
CHAIN_NAME="$(python3 - "$TMP_DIR/health.json" <<'PY'
import json,sys
print((json.load(open(sys.argv[1])) or {}).get("chain","unknown"))
PY
)"
CHAIN_FAMILY_LC="$(printf "%s" "$CHAIN_FAMILY" | tr '[:upper:]' '[:lower:]')"
echo "chain_family=$CHAIN_FAMILY chain=$CHAIN_NAME"

echo "Step 1: heartbeat should update"
curl -fsS "$METRICS_URL" >"$TMP_DIR/metrics_a.prom" || fail "metrics endpoint unreachable at $METRICS_URL"
HB_A="$(metric_sum "mevbot_heartbeat_ts" "$TMP_DIR/metrics_a.prom")"
sleep "${VERIFY_HEARTBEAT_WAIT_S:-12}"
curl -fsS "$METRICS_URL" >"$TMP_DIR/metrics_b.prom" || fail "metrics endpoint unreachable after wait"
HB_B="$(metric_sum "mevbot_heartbeat_ts" "$TMP_DIR/metrics_b.prom")"
python3 - "$HB_A" "$HB_B" <<'PY' || fail "heartbeat metric did not move" "Check runtime monitor loop logs in mev-bot and ensure /metrics is served by live process."
import sys
a=float(sys.argv[1]); b=float(sys.argv[2])
if not (b > a and a > 0):
    raise SystemExit(f"heartbeat_not_advancing a={a} b={b}")
print(f"OK heartbeat advanced: {a:.0f} -> {b:.0f}")
PY

echo "Step 2: chain head/slot should advance"
METRIC_NAME="mevbot_chain_head"
if [[ "$CHAIN_FAMILY_LC" == "sol" ]]; then
  METRIC_NAME="mevbot_chain_slot"
fi
H_A="$(metric_sum "$METRIC_NAME" "$TMP_DIR/metrics_b.prom")"
sleep "${VERIFY_CHAIN_ADVANCE_WAIT_S:-35}"
curl -fsS "$METRICS_URL" >"$TMP_DIR/metrics_c.prom" || fail "metrics endpoint unreachable during chain advance check"
H_B="$(metric_sum "$METRIC_NAME" "$TMP_DIR/metrics_c.prom")"
python3 - "$METRIC_NAME" "$H_A" "$H_B" <<'PY' || fail "$METRIC_NAME did not advance" "Verify RPC connectivity in /health and check chain observe logs in mev-bot."
import sys
m=sys.argv[1]; a=float(sys.argv[2]); b=float(sys.argv[3])
if a <= 0 or b <= 0:
    raise SystemExit(f"{m}_zero_values a={a} b={b}")
if b <= a:
    raise SystemExit(f"{m}_not_advancing a={a} b={b}")
print(f"OK {m} advanced: {a:.0f} -> {b:.0f}")
PY

echo "Step 3: counters should increase after synthetic stream events"
STREAM_OBS_A="$(metric_sum "mevbot_stream_events_observed_total" "$TMP_DIR/metrics_c.prom")"
SEEN_A="$(metric_sum "mevbot_opportunities_seen_total" "$TMP_DIR/metrics_c.prom")"
PIPE_A="$(metric_sum "mevbot_candidate_pipeline_seen_total" "$TMP_DIR/metrics_c.prom")"

STREAM_NAME="${REDIS_STREAM:-mempool:pending:txs}"
for i in 1 2 3; do
  HASH_SUFFIX="$(printf "%064x" "$(( $(date +%s) + i ))")"
  TX_HASH="0x${HASH_SUFFIX:0:64}"
  TS_MS="$(( $(date +%s) * 1000 ))"
  ${COMPOSE[@]} exec -T redis sh -lc "redis-cli XADD '$STREAM_NAME' '*' tx '$TX_HASH' ts_ms '$TS_MS' selector '0x'" >/dev/null \
    || fail "failed to inject redis stream event" "Run: docker compose logs --tail=200 redis candidate-pipeline"
done
sleep "${VERIFY_COUNTER_WAIT_S:-18}"
curl -fsS "$METRICS_URL" >"$TMP_DIR/metrics_d.prom" || fail "metrics endpoint unreachable after stream injection"

STREAM_OBS_B="$(metric_sum "mevbot_stream_events_observed_total" "$TMP_DIR/metrics_d.prom")"
SEEN_B="$(metric_sum "mevbot_opportunities_seen_total" "$TMP_DIR/metrics_d.prom")"
PIPE_B="$(metric_sum "mevbot_candidate_pipeline_seen_total" "$TMP_DIR/metrics_d.prom")"

python3 - "$STREAM_OBS_A" "$STREAM_OBS_B" "$SEEN_A" "$SEEN_B" "$PIPE_A" "$PIPE_B" <<'PY' || fail "no counter increase after synthetic events" "Check stream observer logs, candidate-pipeline consumer group, and Prometheus multiprocess setup."
import sys
stream_a=float(sys.argv[1]); stream_b=float(sys.argv[2]); seen_a=float(sys.argv[3]); seen_b=float(sys.argv[4]); pipe_a=float(sys.argv[5]); pipe_b=float(sys.argv[6])
ok = (stream_b > stream_a) or (seen_b > seen_a) or (pipe_b > pipe_a)
if not ok:
    raise SystemExit(
        f"counter_not_advancing stream_observed={stream_a}->{stream_b} opportunities_seen={seen_a}->{seen_b} pipeline_seen={pipe_a}->{pipe_b}"
    )
print(
    f"OK counters advanced: stream_observed {stream_a}->{stream_b}, opportunities_seen {seen_a}->{seen_b}, pipeline_seen {pipe_a}->{pipe_b}"
)
PY

echo "PASS: blockchain -> bot -> metrics data flow is live."

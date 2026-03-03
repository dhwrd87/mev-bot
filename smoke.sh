#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$ROOT_DIR/docker/docker-compose.yml" -f "$ROOT_DIR/docker/docker-compose.override.yml" --env-file "$ROOT_DIR/.env.runtime")
TMP_DIR="${TMPDIR:-/tmp}/mev-smoke"
mkdir -p "$TMP_DIR"

log() { printf "\n==> %s\n" "$*"; }
warn() { printf "WARN: %s\n" "$*" >&2; }
fail() {
  local msg="$1"
  local next="${2:-Check docker compose logs for mev-bot, mempool-producer, mempool-consumer, candidate-pipeline.}"
  echo "FAIL: $msg" >&2
  echo "Next: $next" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 2
  }
}

service_health() {
  local service="$1"
  local cid
  cid="$(${COMPOSE[@]} ps -q "$service")"
  if [[ -z "$cid" ]]; then
    echo "missing"
    return 1
  fi
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid"
}

pg_scalar() {
  local sql="$1"
  local out=""
  out="$(${COMPOSE[@]} exec -T -e SMOKE_SQL="$sql" postgres sh -lc '
set -e
for U in "${POSTGRES_USER:-}" mevbot mev_user; do
  [ -n "$U" ] || continue
  for D in "${POSTGRES_DB:-}" mevbot mev_bot; do
    [ -n "$D" ] || continue
    if echo "$SMOKE_SQL" | psql -U "$U" -d "$D" -tA >/tmp/.smoke_pg_out 2>/tmp/.smoke_pg_err; then
      cat /tmp/.smoke_pg_out
      exit 0
    fi
  done
done
cat /tmp/.smoke_pg_err >&2 || true
exit 1
' 2>/dev/null || true)"
  [[ -n "$out" ]] || return 1
  printf "%s" "$out"
}

require_cmd docker
require_cmd curl
require_cmd python3

if [[ ! -f "$ROOT_DIR/.env.runtime" ]]; then
  echo "Missing env file: $ROOT_DIR/.env.runtime" >&2
  exit 2
fi

log "Prepare runtime dirs"
mkdir -p "$ROOT_DIR/tmp/prom_mp"
rm -f "$ROOT_DIR"/tmp/prom_mp/*.db "$ROOT_DIR"/tmp/prom_mp/*.lck 2>/dev/null || true

log "Stack boot"
${COMPOSE[@]} up -d --build >/dev/null || fail "docker compose up -d --build failed" "Run: docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime logs --tail=200"

log "API & endpoint health"
api_check() {
  local path="$1"
  local out="$TMP_DIR/$(echo "$path" | tr '/?&=' '_').out"
  local code="000"
  for _ in $(seq 1 25); do
    code="$(curl -sS --max-time 8 -o "$out" -w '%{http_code}' "http://127.0.0.1:8000$path" || true)"
    [[ "$code" == "200" ]] && break
    sleep 1
  done
  [[ "$code" == "200" ]] || fail "API endpoint $path returned HTTP $code" "Inspect $out and then run: docker compose logs --tail=200 mev-bot"
  echo "OK: $path returned HTTP 200"
}

api_check "/health"
api_check "/debug/mempool"
api_check "/debug/candidates?limit=10"

log "Metrics endpoint check"
METRICS_OUT="$TMP_DIR/_metrics.out"
METRICS_CODE="000"
for _ in $(seq 1 25); do
  METRICS_CODE="$(curl -sS --max-time 8 -o "$METRICS_OUT" -w '%{http_code}' "http://127.0.0.1:8000/metrics" || true)"
  [[ "$METRICS_CODE" == "200" || "$METRICS_CODE" == "500" ]] && break
  sleep 1
done
if [[ "$METRICS_CODE" == "200" ]]; then
  echo "OK: /metrics returned HTTP 200"
elif [[ "$METRICS_CODE" == "500" ]]; then
  warn "/metrics returned HTTP 500 (endpoint reachable but collector errored)."
  warn "Next: inspect mev-bot logs for prometheus multiprocess decode errors."
else
  fail "/metrics endpoint unreachable (HTTP $METRICS_CODE)" "Inspect $METRICS_OUT and run: docker compose logs --tail=200 mev-bot"
fi

log "Infra health"
REDIS_HEALTH="$(service_health redis)"
POSTGRES_HEALTH="$(service_health postgres)"
echo "redis=$REDIS_HEALTH"
echo "postgres=$POSTGRES_HEALTH"

[[ "$REDIS_HEALTH" == "healthy" ]] || fail "redis is not healthy" "Run: docker compose logs --tail=200 redis"
[[ "$POSTGRES_HEALTH" == "healthy" ]] || fail "postgres is not healthy" "Run: docker compose logs --tail=200 postgres"

log "Redis stream write/read checks"
SMOKE_MARKER="smoke-$(date +%s)-$$"
SMOKE_ID="$(${COMPOSE[@]} exec -T redis sh -lc "redis-cli XADD smoke:probe '*' probe '$SMOKE_MARKER'" | tr -d '\r\n' || true)"
[[ -n "$SMOKE_ID" ]] || fail "redis XADD probe failed" "Run: docker compose exec -T redis redis-cli ping"
SMOKE_READ="$(${COMPOSE[@]} exec -T redis sh -lc "redis-cli XRANGE smoke:probe '$SMOKE_ID' '$SMOKE_ID'" || true)"
echo "$SMOKE_READ" | grep -q "$SMOKE_MARKER" || fail "redis XRANGE probe failed to read inserted stream entry" "Run: docker compose exec -T redis redis-cli XRANGE smoke:probe - + COUNT 5"
echo "redis_probe_id=$SMOKE_ID marker=$SMOKE_MARKER"

log "RPC/WS config snapshot"
${COMPOSE[@]} exec -T mempool-consumer python3 - <<'PY'
import os
from bot.core.chain_config import get_chain_config

cfg = get_chain_config()
print(f"RPC_RPS={os.getenv('RPC_RPS', '8')}")
print(f"RPC_BURST={os.getenv('RPC_BURST', '16')}")
print("RPC_HTTP_PRIMARY=" + cfg.rpc_http)
print("RPC_HTTP_BACKUPS=" + ",".join(cfg.rpc_http_backups))
print("WS_ENDPOINTS=" + ",".join(cfg.ws_endpoints))
PY

log "Worker logs (producer/consumer)"
PRODUCER_LOGS="$(${COMPOSE[@]} logs --tail=200 mempool-producer || true)"
CONSUMER_LOGS="$(${COMPOSE[@]} logs --tail=200 mempool-consumer || true)"

echo "$PRODUCER_LOGS" | tail -n 20
echo "$CONSUMER_LOGS" | tail -n 20

echo "$PRODUCER_LOGS" | grep -Eq "connected endpoint=|producer start" || {
  fail "producer logs missing connection/start markers" "Run: docker compose logs --tail=200 mempool-producer"
}

echo "$CONSUMER_LOGS" | grep -Eq "Starting mempool_consumer|consumer_stats" || {
  fail "consumer logs missing startup/stats markers" "Run: docker compose logs --tail=200 mempool-consumer"
}

echo "$PRODUCER_LOGS$CONSUMER_LOGS" | grep -Eq "Traceback \(most recent call last\)|FATAL|CRITICAL" && {
  fail "fatal errors found in producer/consumer logs" "Run: docker compose logs --tail=200 mempool-producer mempool-consumer"
}

log "DB pipeline persistence checks"
EVENTS_BEFORE="$(pg_scalar "select count(*) from mempool_events;" | tr -d '[:space:]' || true)"
[[ -n "${EVENTS_BEFORE:-}" ]] || fail "failed querying mempool_events count" "Run: docker compose exec -T postgres psql -U mevbot -d mevbot -c 'select 1'"
sleep 10
EVENTS_AFTER="$(pg_scalar "select count(*) from mempool_events;" | tr -d '[:space:]' || true)"
TX_COUNT="$(pg_scalar "select count(*) from mempool_tx;" | tr -d '[:space:]' || true)"

echo "mempool_events before=$EVENTS_BEFORE after=$EVENTS_AFTER delta=$((EVENTS_AFTER-EVENTS_BEFORE))"
echo "mempool_tx count=$TX_COUNT"

[[ "$EVENTS_AFTER" -gt "$EVENTS_BEFORE" ]] || fail "mempool_events did not increase" "Check ws producer connectivity and consumer group lag: curl http://127.0.0.1:8000/debug/mempool"
[[ "$TX_COUNT" -gt 0 ]] || fail "mempool_tx count is 0" "Check consumer RPC fetch path and rate limiting logs in mempool-consumer."

log "Paper mode + migration checks"
CAND_TABLE="$(pg_scalar "select to_regclass('public.candidates');" | tr -d '[:space:]' || true)"
echo "candidates_table=$CAND_TABLE"
[[ "$CAND_TABLE" == "candidates" || "$CAND_TABLE" == "public.candidates" ]] || fail "candidates table missing (migrations not applied)" "Run migrations and inspect: scripts/migrate.py"

CAND_HTTP="$(curl -sS --max-time 5 -o "$TMP_DIR/candidates.out" -w '%{http_code}' http://127.0.0.1:8000/candidates || true)"
echo "candidates_http=$CAND_HTTP"
[[ "$CAND_HTTP" == "200" ]] || fail "/candidates endpoint unhealthy" "Inspect $TMP_DIR/candidates.out and run: docker compose logs --tail=200 mev-bot"

CAND_BEFORE="$(pg_scalar "select count(*) from candidates;" | tr -d '[:space:]' || true)"
sleep 10
CAND_AFTER="$(pg_scalar "select count(*) from candidates;" | tr -d '[:space:]' || true)"
echo "candidates before=$CAND_BEFORE after=$CAND_AFTER delta=$((CAND_AFTER-CAND_BEFORE))"
[[ "$CAND_AFTER" -gt "$CAND_BEFORE" ]] || fail "candidate pipeline did not insert new rows in paper mode" "Run: docker compose logs --tail=200 candidate-pipeline and verify detector/sim logs."

OUTCOME_TABLE="$(pg_scalar "select to_regclass('public.candidates_outcomes');" | tr -d '[:space:]' || true)"
echo "candidates_outcomes_table=$OUTCOME_TABLE"
[[ "$OUTCOME_TABLE" == "candidates_outcomes" || "$OUTCOME_TABLE" == "public.candidates_outcomes" ]] || fail "candidates_outcomes table missing" "Run migrations and verify evaluator schema."

PAPER_HTTP="$(curl -sS --max-time 5 -o "$TMP_DIR/paper_report.out" -w '%{http_code}' http://127.0.0.1:8000/paper_report || true)"
echo "paper_report_http=$PAPER_HTTP"
[[ "$PAPER_HTTP" == "200" ]] || fail "/paper_report endpoint unhealthy" "Inspect $TMP_DIR/paper_report.out and run: docker compose logs --tail=200 mev-bot"

log "Prometheus target health"
PROM_TARGETS_JSON="$TMP_DIR/prom_targets.json"
PROM_CODE="$(curl -sS --max-time 8 -o "$PROM_TARGETS_JSON" -w '%{http_code}' http://127.0.0.1:9090/api/v1/targets || true)"
[[ "$PROM_CODE" == "200" ]] || fail "Prometheus targets API returned HTTP $PROM_CODE" "Run: docker compose logs --tail=200 prometheus"
python3 - "$PROM_TARGETS_JSON" <<'PY' || fail "Prometheus has unhealthy scrape targets" "Open http://127.0.0.1:9090/targets and inspect down jobs."
import json, sys
p = sys.argv[1]
data = json.load(open(p))
if data.get("status") != "success":
    raise SystemExit("prom-status-not-success")
active = (data.get("data") or {}).get("activeTargets") or []
if not active:
    raise SystemExit("no-active-targets")
down = []
for t in active:
    health = (t.get("health") or "").lower()
    labels = t.get("labels") or {}
    if health != "up":
        down.append(labels.get("job") or t.get("scrapeUrl") or "unknown")
if down:
    raise SystemExit("down-targets:" + ",".join(down))
jobs = sorted({(t.get("labels") or {}).get("job", "unknown") for t in active})
print("prometheus_targets_ok jobs=" + ",".join(jobs))
PY

log "Alertmanager config check"
AM_STATUS_JSON="$TMP_DIR/alertmanager_status.json"
AM_CODE="$(curl -sS --max-time 8 -o "$AM_STATUS_JSON" -w '%{http_code}' http://127.0.0.1:9093/api/v2/status || true)"
[[ "$AM_CODE" == "200" ]] || fail "Alertmanager status API returned HTTP $AM_CODE" "Run: docker compose logs --tail=200 alertmanager"
python3 - "$AM_STATUS_JSON" <<'PY' || fail "Alertmanager receiver config missing discord-relay webhook" "Run: docker compose exec -T alertmanager cat /etc/alertmanager/alertmanager.yml"
import json, sys
p = sys.argv[1]
data = json.load(open(p))
cfg = (data.get("config") or {}).get("original", "")
if "receiver: discord-relay" not in cfg or "webhook_configs:" not in cfg:
    raise SystemExit("missing-relay-config")
print("alertmanager_receiver_ok=discord-relay")
PY

log "Grafana reachability (optional)"
GRAFANA_CODE="$(curl -sS --max-time 5 -o "$TMP_DIR/grafana_health.out" -w '%{http_code}' http://127.0.0.1:3000/api/health || true)"
if [[ "$GRAFANA_CODE" == "200" ]]; then
  echo "grafana_http=200"
else
  warn "Grafana not reachable (HTTP $GRAFANA_CODE). Optional check only."
  warn "Next: run docker compose logs --tail=200 grafana"
fi

log "Smoke checks passed"

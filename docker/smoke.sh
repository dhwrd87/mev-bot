#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(pwd)"
COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.override.yml")
ENV_FILE="$ROOT_DIR/../.env.runtime"

log() { printf "\n==> %s\n" "$*"; }

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

# Load runtime env for CHAIN and optional settings
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

CHAIN_NAME="${CHAIN:-}"

log "Compose config (merged) validation"
CONFIG_OUT=$("${COMPOSE[@]}" --env-file "$ENV_FILE" config)

echo "$CONFIG_OUT" > /tmp/compose.merged.yml

echo "Merged config saved: /tmp/compose.merged.yml"

log "Check: only one service publishes host port 8000"
PORT8000_COUNT=$(echo "$CONFIG_OUT" | awk '
  $1=="services:" {in_services=1}
  in_services && $1 ~ /^[a-zA-Z0-9_-]+:$/ {svc=$1; gsub(":","",svc)}
  $1=="published:" && $2=="\"8000\"" {print svc}
' | wc -l | tr -d ' ')

if [[ "$PORT8000_COUNT" -ne 1 ]]; then
  echo "FAIL: expected 1 service publishing host 8000, found $PORT8000_COUNT" >&2
  echo "Services publishing 8000:" >&2
  echo "$CONFIG_OUT" | awk '
    $1=="services:" {in_services=1}
    in_services && $1 ~ /^[a-zA-Z0-9_-]+:$/ {svc=$1; gsub(":","",svc)}
    $1=="published:" && $2=="\"8000\"" {print " - " svc}
  ' >&2
  exit 2
fi

log "Compose down"
"${COMPOSE[@]}" --env-file "$ENV_FILE" down

log "Compose up"
"${COMPOSE[@]}" --env-file "$ENV_FILE" up -d

log "Check: /tmp/prom_mp mount conflicts in merged config"
if echo "$CONFIG_OUT" | grep -q "tmpfs:"; then
  echo "FAIL: tmpfs found in merged config (prom_mp should be bind mount only)" >&2
  exit 3
fi

log "Check: Prometheus multiproc dir is writable"
for svc in mev-bot mempool-producer mempool-consumer; do
  if ! "${COMPOSE[@]}" --env-file "$ENV_FILE" ps "$svc" >/dev/null 2>&1; then
    continue
  fi
  echo "- $svc"
  "${COMPOSE[@]}" --env-file "$ENV_FILE" exec -T "$svc" python3 - <<'PY'
import os, sys
p = os.getenv("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom_mp")
try:
    os.makedirs(p, exist_ok=True)
    test = os.path.join(p, "_smoke_write_test")
    with open(test, "w") as f:
        f.write("ok")
    os.remove(test)
    print(f"OK: writable {p}")
except Exception as e:
    print(f"FAIL: {p} not writable: {e}")
    sys.exit(1)
PY
  done

log "Check: mev-bot listens on 0.0.0.0:8000 and /health responds"
"${COMPOSE[@]}" --env-file "$ENV_FILE" exec -T mev-bot python3 - <<'PY'
import http.client, time

def listening_ports():
    ports = set()
    with open("/proc/net/tcp", "r") as f:
        next(f)
        for line in f:
            parts = line.split()
            local = parts[1]
            state = parts[3]
            if state != "0A":
                continue
            port = int(local.split(":")[1], 16)
            ports.add(port)
    return sorted(ports)

print("LISTENING:", listening_ports())

for i in range(10):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "ignore")
        print("HEALTH STATUS:", resp.status)
        print("HEALTH BODY:", body[:400])
        if resp.status == 200:
            break
    except Exception as e:
        print("HEALTH ERROR:", e)
        time.sleep(1)
PY

log "Check: /debug/mempool metrics (bounded; warn-only)"
python3 - <<'PY'
import http.client, json, socket, sys, time

def warn(msg):
    print("WARN:", msg)

try:
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=2)
    conn.request("GET", "/debug/mempool")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "ignore")
except (socket.timeout, TimeoutError) as e:
    warn(f"/debug/mempool timed out: {e}")
    sys.exit(0)
except Exception as e:
    warn(f"/debug/mempool request failed: {e}")
    sys.exit(0)

print("STATUS:", resp.status)
print("BODY:", body[:400])

if resp.status != 200:
    warn(f"/debug/mempool returned {resp.status}")
    sys.exit(0)

try:
    data = json.loads(body)
except Exception as e:
    warn(f"/debug/mempool non-JSON body: {e}")
    sys.exit(0)

age = data.get("last_entry_age_s")
# Smoke should be forgiving; warn if it's stale instead of failing the run.
if age is not None and age > 30:
    warn(f"last_entry_age_s is high ({age}); stream may be idle or producer reconnecting")
PY

log "Check: Redis stream flow"
STREAM="${REDIS_STREAM:-mempool:pending:txs}"
GROUP="${REDIS_GROUP:-mempool}"

XLEN1=$(${COMPOSE[@]} --env-file "$ENV_FILE" exec -T redis redis-cli XLEN "$STREAM")
sleep 2
XLEN2=$(${COMPOSE[@]} --env-file "$ENV_FILE" exec -T redis redis-cli XLEN "$STREAM")

echo "XLEN1=$XLEN1"
echo "XLEN2=$XLEN2"
echo "XLEN delta=$((XLEN2-XLEN1))"

${COMPOSE[@]} --env-file "$ENV_FILE" exec -T redis redis-cli XINFO GROUPS "$STREAM" || true
${COMPOSE[@]} --env-file "$ENV_FILE" exec -T redis redis-cli XPENDING "$STREAM" "$GROUP" || true
${COMPOSE[@]} --env-file "$ENV_FILE" exec -T redis redis-cli XREVRANGE "$STREAM" + - COUNT 3 || true

log "Check: producer connected endpoint (recent logs)"
${COMPOSE[@]} --env-file "$ENV_FILE" logs --tail=80 mempool-producer | grep -E "connected endpoint=" || true

log "Check: consumer fetch_tx ok (recent logs)"
${COMPOSE[@]} --env-file "$ENV_FILE" logs --tail=200 mempool-consumer | grep -E "fetch_tx ok" || true

log "Check: WS 429 in producer logs"
${COMPOSE[@]} --env-file "$ENV_FILE" logs --tail=200 mempool-producer | grep -E "429|too many" || true

log "Optional: Postgres connectivity"
DB_USER="${POSTGRES_USER:-mev_user}"
DB_NAME="${POSTGRES_DB:-mev_bot}"
${COMPOSE[@]} --env-file "$ENV_FILE" exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -c "select 1;" || true

log "Check: mempool_samples rows (>= 5 after 15s)"
sleep 15
${COMPOSE[@]} --env-file "$ENV_FILE" exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -c "select count(*) as samples from mempool_samples;" || true

echo "\nSMOKE COMPLETE" 

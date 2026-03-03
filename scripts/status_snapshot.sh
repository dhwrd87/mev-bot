#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_DIR="$ROOT_DIR/docker"
ENV_FILE="$ROOT_DIR/.env.runtime"
COMPOSE=(docker compose -f "$DOCKER_DIR/docker-compose.yml" -f "$DOCKER_DIR/docker-compose.override.yml" --env-file "$ENV_FILE")

CHECKS_JSON="/tmp/status_checks.json"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

# Build baseline status (from BUILD_BOARD)
python3 "$ROOT_DIR/scripts/status_tools.py" build

# Run smoke
"$DOCKER_DIR/smoke.sh" | tee /tmp/smoke.out

# /health
HEALTH_OUT=$(python3 - <<'PY'
import http.client
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
conn.request("GET", "/health")
resp = conn.getresponse()
body = resp.read().decode("utf-8","ignore")
print(resp.status)
print(body[:400])
PY
)

# Redis XLEN delta
STREAM="${REDIS_STREAM:-mempool:pending:txs}"
XLEN1=$(${COMPOSE[@]} exec -T redis redis-cli XLEN "$STREAM")
sleep 2
XLEN2=$(${COMPOSE[@]} exec -T redis redis-cli XLEN "$STREAM")

# Producer connected endpoint
PROD_LOG=$(${COMPOSE[@]} logs --tail=100 mempool-producer | grep -E "connected endpoint=" | tail -n 1 || true)

# Consumer fetch_tx ok
CONS_LOG=$(${COMPOSE[@]} logs --tail=200 mempool-consumer | grep -E "fetch_tx ok" | tail -n 1 || true)

# Postgres SELECT 1
DB_USER="${POSTGRES_USER:-mev_user}"
DB_NAME="${POSTGRES_DB:-mev_bot}"
DB_OUT=$(${COMPOSE[@]} exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -c "select 1;" 2>&1 || true)

python3 - <<PY
import json, time
checks = {
  "compose_smoke": {"ok": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": open("/tmp/smoke.out").read()[-2000:]},
  "health": {"ok": ${HEALTH_OUT%%$'\n'*} == 200, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": """${HEALTH_OUT}"""},
  "redis_xlen": {"ok": int("$XLEN2") >= int("$XLEN1"), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": f"XLEN1=$XLEN1 XLEN2=$XLEN2"},
  "producer_connected": {"ok": len("""$PROD_LOG""") > 0, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": """$PROD_LOG"""},
  "consumer_fetch": {"ok": len("""$CONS_LOG""") > 0, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": """$CONS_LOG"""},
  "postgres": {"ok": "(1 row)" in """$DB_OUT""", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "output": """$DB_OUT"""},
}
with open("$CHECKS_JSON", "w") as f:
  json.dump(checks, f, indent=2)
PY

python3 "$ROOT_DIR/scripts/status_tools.py" snapshot --checks "$CHECKS_JSON"

python3 "$ROOT_DIR/scripts/status_tools.py" render

echo "Updated STATUS.json and STATUS.md"

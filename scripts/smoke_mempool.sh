#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT_DIR/docker/docker-compose.yml")

STREAM="${REDIS_STREAM:-mempool:pending:txs}"
GROUP="${REDIS_GROUP:-mempool}"

# Load runtime env for CHAIN + provider toggles
set -a
# shellcheck disable=SC1090
source "$ROOT_DIR/.env.runtime"
set +a

exec_redis() {
  "${COMPOSE[@]}" exec -T redis redis-cli "$@"
}

echo "stream: $STREAM"
echo "group:  $GROUP"

echo ""
echo "chain config:"
PYTHONPATH="$ROOT_DIR" python3 - <<'PY'
from bot.core.chain_config import get_chain_config
c = get_chain_config()
print(f"CHAIN={c.chain} CHAIN_ID={c.chain_id}")
print("WS_ENDPOINTS=" + ",".join(c.ws_endpoints))
print("RPC_HTTP=" + c.rpc_http)
print("RPC_HTTP_BACKUPS=" + ",".join(c.rpc_http_backups))
PY

echo "\nXLEN (sample 1)"
XLEN1=$(exec_redis XLEN "$STREAM")
echo "$XLEN1"

sleep 2

echo "\nXLEN (sample 2)"
XLEN2=$(exec_redis XLEN "$STREAM")
echo "$XLEN2"
echo "XLEN delta: $((XLEN2-XLEN1))"

echo "\nXINFO STREAM"
exec_redis XINFO STREAM "$STREAM"

echo "\nXINFO GROUPS"
exec_redis XINFO GROUPS "$STREAM"

echo "\nXREVRANGE (last 3)"
exec_redis XREVRANGE "$STREAM" + - COUNT 3

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.runtime}"

cd "$ROOT_DIR/docker"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile test up -d redis postgres anvil mev-bot-test
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile test run --rm \
  -e BOT_BASE_URL=http://mev-bot-test:8000 \
  -e ANVIL_RPC_URL=http://anvil:8545 \
  tests pytest -p pytest_asyncio.plugin -q -m integration "$@"

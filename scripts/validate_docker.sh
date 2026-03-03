#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.runtime}"

cd "$ROOT_DIR/docker"

echo "==> Building local/mev-bot image"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build

echo "==> Starting required integration services"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d redis postgres

if [[ "${WITH_MEV_BOT:-0}" == "1" ]]; then
  echo "==> Starting mev-bot service (WITH_MEV_BOT=1)"
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d mev-bot
fi

echo "==> Running unit tests (default: excludes integration)"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile test run --rm tests pytest -p pytest_asyncio.plugin -q -m "not integration"

echo "==> Running integration tests"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile test run --rm tests pytest -p pytest_asyncio.plugin -q -m integration

echo "==> Docker test validation complete"

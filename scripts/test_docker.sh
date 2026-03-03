#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.runtime}"

cd "$ROOT_DIR/docker"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile test run --rm tests pytest -p pytest_asyncio.plugin -q -m "not integration and not e2e" "$@"

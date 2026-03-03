#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.runtime"

for arg in "$@"; do
  if [[ "$arg" == "--env-file" ]]; then
    echo "Do not pass --env-file to this script; it always uses $ENV_FILE" >&2
    exit 2
  fi
done

exec docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/docker/docker-compose.yml" "$@"

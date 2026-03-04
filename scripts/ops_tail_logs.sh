#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yml"
ENV_FILE="$ROOT_DIR/.env.runtime"
COMPOSE_PROFILES=(--profile ops --profile test)

COMPOSE=(docker compose -f "$COMPOSE_FILE" "${COMPOSE_PROFILES[@]}")
if [[ -f "$ENV_FILE" ]]; then
  COMPOSE+=(--env-file "$ENV_FILE")
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Missing compose file: $COMPOSE_FILE" >&2
  exit 1
fi

mapfile -t SERVICES < <("${COMPOSE[@]}" config --services)
if [[ ${#SERVICES[@]} -eq 0 ]]; then
  echo "No services found from docker compose config --services" >&2
  exit 1
fi

declare -A SERVICE_SET=()
for s in "${SERVICES[@]}"; do
  SERVICE_SET["$s"]=1
done

declare -A SELECTED=()

add_service() {
  local svc="$1"
  if [[ -n "${SERVICE_SET[$svc]:-}" ]]; then
    SELECTED["$svc"]=1
  fi
}

expand_token() {
  local token="$1"
  case "$token" in
    api) echo "mev-bot" ;;
    bot) echo "mev-bot" ;;
    discord) echo "discord-operator" ;;
    *) echo "$token" ;;
  esac
}

if [[ $# -eq 0 ]]; then
  for s in "${SERVICES[@]}"; do
    SELECTED["$s"]=1
  done
else
  for raw in "$@"; do
    token="$(echo "$raw" | tr '[:upper:]' '[:lower:]')"
    resolved="$(expand_token "$token")"
    found=0

    if [[ -n "${SERVICE_SET[$resolved]:-}" ]]; then
      add_service "$resolved"
      found=1
    else
      for s in "${SERVICES[@]}"; do
        sl="$(echo "$s" | tr '[:upper:]' '[:lower:]')"
        if [[ "$sl" == *"$resolved"* ]]; then
          add_service "$s"
          found=1
        fi
      done
    fi

    if [[ $found -eq 0 ]]; then
      echo "No service matches filter '$raw'." >&2
      echo "Valid services:" >&2
      printf '  - %s\n' "${SERVICES[@]}" >&2
      exit 2
    fi
  done
fi

mapfile -t MATCHED < <(printf '%s\n' "${!SELECTED[@]}" | sort)
if [[ ${#MATCHED[@]} -eq 0 ]]; then
  echo "No services selected." >&2
  exit 2
fi

echo "Tailing logs for services: ${MATCHED[*]}"
exec "${COMPOSE[@]}" logs -f "${MATCHED[@]}"

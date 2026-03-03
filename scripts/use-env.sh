#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: scripts/use-env.sh sepolia|amoy|mainnet" >&2
  exit 2
fi

TARGET="$1"
case "$TARGET" in
  sepolia|amoy|mainnet) ;;
  *)
    echo "unsupported chain: $TARGET (allowed: sepolia, amoy, mainnet)" >&2
    exit 2
    ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.runtime"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 2
fi

if grep -qE '^CHAIN=' "$ENV_FILE"; then
  sed -i.bak -E "s/^CHAIN=.*/CHAIN=${TARGET}/" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
else
  printf '\nCHAIN=%s\n' "$TARGET" >> "$ENV_FILE"
fi

echo "updated .env.runtime: CHAIN=$TARGET"

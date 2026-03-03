#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv. Run ./scripts/dev_setup.sh first." >&2
  exit 1
fi

export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
exec .venv/bin/python -m pytest -p pytest_asyncio "$@"

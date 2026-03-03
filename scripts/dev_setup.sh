#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY_BIN="${PYTHON_BIN:-python3.12}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "WARN: $PY_BIN not found; falling back to python3" >&2
  PY_BIN="python3"
fi

VENV_DIR="${VENV_DIR:-.venv}"

echo "==> Creating virtual environment: $VENV_DIR (python=$PY_BIN)"
"$PY_BIN" -m venv "$VENV_DIR"

echo "==> Installing pinned dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel || \
  echo "WARN: pip/setuptools upgrade skipped (likely offline), continuing..."
"$VENV_DIR/bin/python" -m pip install -r requirements.txt -r requirements-dev.txt

echo "==> Setup complete"
echo "Activate: source $VENV_DIR/bin/activate"
echo "Run operator: python -m ops.discord_operator"

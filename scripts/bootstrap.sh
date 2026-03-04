#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.runtime}"
REF_FILE="${REF_FILE:-$ROOT_DIR/.env.example}"

log() { printf "\n==> %s\n" "$*"; }
fail() {
  echo "BOOTSTRAP_FAIL: $*" >&2
  exit 1
}

if [[ ! -f "$ENV_FILE" ]]; then
  [[ -f "$REF_FILE" ]] || fail "missing reference env file: $REF_FILE"
  cp "$REF_FILE" "$ENV_FILE"
  echo "Created $ENV_FILE from $REF_FILE"
fi

log "Hydrate missing env keys from reference"
python3 - "$ENV_FILE" "$REF_FILE" <<'PY'
from pathlib import Path
import re
import sys

env_path = Path(sys.argv[1])
ref_path = Path(sys.argv[2])
env_text = env_path.read_text(encoding="utf-8")
env_keys = set()
for raw in env_text.splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    env_keys.add(line.split("=", 1)[0].strip())

missing = []
pat = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
for raw in ref_path.read_text(encoding="utf-8").splitlines():
    m = pat.match(raw)
    if not m:
        continue
    k = m.group(1)
    if k in env_keys:
        continue
    missing.append(raw.rstrip())

if missing:
    with env_path.open("a", encoding="utf-8") as f:
        f.write("\n# Added by scripts/bootstrap.sh from .env.example\n")
        for line in missing:
            f.write(line + "\n")
    print(f"added_missing_keys={len(missing)}")
else:
    print("added_missing_keys=0")
PY

log "Validate environment"
python3 "$ROOT_DIR/scripts/validate_env.py" --env-file "$ENV_FILE" --reference "$REF_FILE"

log "Start docker stack"
"$ROOT_DIR/scripts/up.sh" up -d --build

log "Run DB migrations inside mev-bot container"
for i in $(seq 1 30); do
  if "$ROOT_DIR/scripts/up.sh" exec -T mev-bot python3 scripts/migrate.py >/tmp/bootstrap_migrate.out 2>/tmp/bootstrap_migrate.err; then
    cat /tmp/bootstrap_migrate.out
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    cat /tmp/bootstrap_migrate.err >&2 || true
    fail "migrations failed after retries"
  fi
  sleep 2
done

log "Run existing verification scripts"
"$ROOT_DIR/smoke.sh"
"$ROOT_DIR/scripts/verify_metrics.sh"

log "Final health check"
code="$(curl -sS --max-time 10 -o /tmp/bootstrap_health.out -w '%{http_code}' http://127.0.0.1:8000/health || true)"
[[ "$code" == "200" ]] || {
  cat /tmp/bootstrap_health.out >&2 || true
  fail "health endpoint returned HTTP $code"
}

echo "BOOTSTRAP_OK"

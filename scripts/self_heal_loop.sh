#!/usr/bin/env bash
set -euo pipefail

MAX_ITERATIONS="${MAX_ITERATIONS:-10}"
ITERATION=0
COMPOSE_FILE="${COMPOSE_FILE:-docker/docker-compose.yml}"

while [ "$ITERATION" -lt "$MAX_ITERATIONS" ]; do
  echo "=== Iteration $((ITERATION+1)) of $MAX_ITERATIONS ==="

  if python3 scripts/validate_trading_engine.py; then
    echo "✓ All checks passed!"
    break
  fi

  echo "✗ Validation failed. Attempting auto-fix..."

  if ! docker compose -f "$COMPOSE_FILE" ps | grep opportunity-processor | grep -q "Up"; then
    echo "Restarting opportunity-processor..."
    docker compose -f "$COMPOSE_FILE" restart opportunity-processor || true
    sleep 10
  fi

  if ! docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U mev_user -d mev_bot -c "SELECT 1 FROM trades LIMIT 1;" >/dev/null 2>&1; then
    echo "Running migrations..."
    docker compose -f "$COMPOSE_FILE" exec -T mev-bot python3 scripts/migrate.py || true
    sleep 5
  fi

  if ! curl -fsS http://localhost:9100/metrics | grep -q "mevbot_opportunities"; then
    echo "Restarting mev-bot..."
    docker compose -f "$COMPOSE_FILE" restart mev-bot || true
    sleep 10
  fi

  ITERATION=$((ITERATION+1))
  sleep 10
done

if [ "$ITERATION" -eq "$MAX_ITERATIONS" ]; then
  echo "✗ Failed to auto-heal after $MAX_ITERATIONS iterations"
  echo "Manual intervention required"
  exit 1
fi

echo "✓✓✓ System fully validated and operational!"

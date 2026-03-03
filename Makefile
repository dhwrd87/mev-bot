SHELL := /bin/bash

VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip
PYTHON_CMD := $(shell if [ -x "$(PYTHON)" ]; then echo "$(PYTHON)"; else echo "python3"; fi)

.DEFAULT_GOAL := help

.PHONY: help venv deps lint typecheck test test-v test-warnings test-verbose compose-up compose-down smoke sim-smoke status pause-test redis-peek consumer-smoke \
	gap1-proof gap2-proof gap3-proof \
	proof-all \
	proof-bootstrap proof-typed-config proof-postgres-migrations proof-telemetry-baseline proof-mempool-monitor \
	proof-ws-redis-handoff \
	proof-permit2 proof-exact-output-swap proof-private-orderflow proof-stealth-triggers proof-stealth-e2e \
	proof-sniper-sandwich proof-backrun-calculator proof-bundle-builder proof-builder-submissions proof-hunter-e2e \
	proof-strategy-orchestrator proof-adaptive-risk proof-kill-switch-api proof-gas-policy proof-alerting \
	proof-grafana-dashboards proof-nightly-etl-duckdb proof-weekly-analytics-report proof-secrets-hardening \
	proof-access-control proof-audit-logging proof-pre-submit-simulation \
	up logs mempool-smoke

help:
	@echo "Targets:"
	@echo "  make venv                         Create virtual environment in $(VENV_DIR)"
	@echo "  make deps                         Install runtime + dev dependencies"
	@echo "  make lint                         Run ruff check and format"
	@echo "  make typecheck                    Run mypy if available"
	@echo "  make test                         Run pytest (quiet)"
	@echo "  make compose-up                   Start docker compose stack"
	@echo "  make compose-down                 Stop docker compose stack"
	@echo "  make up                           Start docker compose stack (uses .env.runtime)"
	@echo "  make logs                         Tail docker compose logs (uses .env.runtime)"
	@echo "  make mempool-smoke                Inspect mempool Redis stream"
	@echo "  make smoke                        Run golden-path smoke (mocked)"
	@echo "  make sim-smoke                    Run simulator smoke (no external deps)"
	@echo "  make status                       Print runtime/system status snapshot"
	@echo "  make pause-test                   Run pause/resume integration test"
	@echo "  make redis-peek                   Verify Redis mempool stream growth and show latest entry"
	@echo "  make consumer-smoke               Verify consumer RPC fetch success appears in logs"
	@echo "  make gap1-proof                   Prove GAP-1 fix (producer wiring)"
	@echo "  make gap2-proof                   Prove GAP-2 fix (WS->Redis publishing)"
	@echo "  make gap3-proof                   Prove GAP-3 fix (consumer RPC env + fetch)"
	@echo "  make proof-all                    Run all proof targets in sequence"
	@echo "  make proof-<task>                 Run deterministic proof for a board task"

venv:
	python3 -m venv $(VENV_DIR)
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt


deps: venv
	@echo "Dependencies installed in $(VENV_DIR)"

lint:
	@if [ -x "$(VENV_BIN)/ruff" ]; then \
		$(VENV_BIN)/ruff check . && $(VENV_BIN)/ruff format .; \
	elif command -v ruff >/dev/null 2>&1; then \
		ruff check . && ruff format .; \
	else \
		echo "ruff not installed"; exit 1; \
	fi

typecheck:
	@if [ -x "$(VENV_BIN)/mypy" ]; then \
		$(VENV_BIN)/mypy bot; \
	elif command -v mypy >/dev/null 2>&1; then \
		mypy bot; \
	else \
		echo "mypy not installed (skipping)"; \
	fi

test:
	@if [ -x "$(PYTHON)" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -p pytest_asyncio; \
	else \
		echo "python not found in $(VENV_DIR). Run 'make venv' first."; exit 1; \
	fi

test-v:
	@if [ -x "$(PYTHON)" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -vv -p pytest_asyncio; \
	else \
		echo "python not found in $(VENV_DIR). Run 'make venv' first."; exit 1; \
	fi

test-warnings:
	@if [ -x "$(PYTHON)" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -p pytest_asyncio -W default; \
	else \
		echo "python not found in $(VENV_DIR). Run 'make venv' first."; exit 1; \
	fi

test-verbose:
	@if [ -x "$(PYTHON)" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -v -p pytest_asyncio; \
	else \
		echo "python not found in $(VENV_DIR). Run 'make venv' first."; exit 1; \
	fi

compose-up:
	cd docker && docker compose up -d

compose-down:
	cd docker && docker compose down

up:
	./scripts/up.sh up -d

logs:
	./scripts/up.sh logs -f

mempool-smoke:
	./scripts/smoke_mempool.sh

smoke:
	@if [ -x "$(PYTHON)" ]; then \
		PYTHONPATH=. $(PYTHON) scripts/golden_path_smoke.py; \
	else \
		echo "python not found in $(VENV_DIR). Run 'make venv' first."; exit 1; \
	fi

sim-smoke:
	@if [ -x "$(VENV_BIN)/python" ]; then \
		PYTHONPATH=. $(VENV_BIN)/python scripts/sim_smoke.py; \
	else \
		PYTHONPATH=. python3 scripts/sim_smoke.py; \
	fi

status:
	PYTHONPATH=. $(PYTHON_CMD) scripts/status.py --write

pause-test:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q tests/integration/test_pause_api.py; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/integration/test_pause_api.py; \
	fi

redis-peek:
	@bash -lc 'before=$$(docker exec mev-redis redis-cli XLEN mempool:pending:txs); sleep 8; after=$$(docker exec mev-redis redis-cli XLEN mempool:pending:txs); delta=$$((after-before)); echo "before=$$before after=$$after delta=$$delta"; test $$delta -gt 0'
	docker exec mev-redis redis-cli XREVRANGE mempool:pending:txs + - COUNT 1

consumer-smoke:
	cd docker && docker compose up -d mempool-producer mempool-consumer redis
	cd docker && docker compose restart mempool-consumer
	@bash -lc 'timeout=40; while [ $$timeout -gt 0 ]; do if (cd docker && docker compose logs --tail=300 mempool-consumer | grep -q "fetch_tx ok"); then (cd docker && docker compose logs --tail=80 mempool-consumer | grep "fetch_tx ok" | tail -n 5); exit 0; fi; sleep 2; timeout=$$((timeout-2)); done; (cd docker && docker compose logs --tail=120 mempool-consumer); exit 1'

gap1-proof:
	cd docker && docker compose config | sed -n '/mempool-producer:/,/^[^ ]/p' | grep -q "ws_to_redis.py"
	docker exec mev-mempool-producer sh -lc 'tr "\0" " " </proc/1/cmdline' | grep -q "ws_to_redis.py"

gap2-proof:
	$(MAKE) compose-up
	$(MAKE) redis-peek

gap3-proof:
	grep -q 'RPC_HTTP      = (os.getenv("RPC_HTTP", "") or os.getenv("RPC_ENDPOINT_PRIMARY", "") or "").strip()' bot/workers/mempool_consumer.py
	$(MAKE) consumer-smoke

proof-all: \
	proof-bootstrap \
	proof-typed-config \
	proof-postgres-migrations \
	proof-telemetry-baseline \
	gap1-proof \
	gap2-proof \
	gap3-proof \
	proof-mempool-monitor \
	proof-ws-redis-handoff \
	proof-permit2 \
	proof-exact-output-swap \
	proof-private-orderflow \
	proof-stealth-triggers \
	proof-stealth-e2e \
	proof-sniper-sandwich \
	proof-backrun-calculator \
	proof-bundle-builder \
	proof-builder-submissions \
	proof-hunter-e2e \
	proof-strategy-orchestrator \
	proof-adaptive-risk \
	proof-kill-switch-api \
	proof-gas-policy \
	proof-alerting \
	proof-grafana-dashboards \
	proof-nightly-etl-duckdb \
	proof-weekly-analytics-report \
	proof-secrets-hardening \
	proof-access-control \
	proof-audit-logging \
	proof-pre-submit-simulation

proof-bootstrap:
	cd docker && docker compose up -d && docker compose ps
	curl -sf http://127.0.0.1:8000/health >/dev/null

proof-typed-config:
	PYTHONPATH=. $(PYTHON_CMD) -c "from bot.config import settings; print('typed-config-ok')"
	PYTHONPATH=. $(PYTHON_CMD) -c "import os; os.environ['POSTGRES_DB']=''; from bot.config import settings; print('env-validated')"

proof-postgres-migrations:
	PYTHONPATH=. $(PYTHON_CMD) scripts/migrate.py
	docker exec mev-db psql -U mevbot -d mevbot -c "\\dt"

proof-telemetry-baseline:
	curl -sf http://127.0.0.1:8000/metrics | head -n 5
	curl -sf http://127.0.0.1:9090/-/healthy

proof-mempool-monitor:
	cd docker && docker compose config | sed -n '/mempool-producer:/,/^[^ ]/p' | grep -q "ws_to_redis.py"
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "mempool or websocket"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "mempool or websocket"; \
	fi
	PYTHONPATH=. $(PYTHON_CMD) scripts/collect_mempool_rate.py

proof-ws-redis-handoff:
	PYTHONPATH=. $(PYTHON_CMD) scripts/validate_ws_redis_handoff.py

proof-permit2:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "permit2"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "permit2"; \
	fi

proof-exact-output-swap:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "exact_output or swap"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "exact_output or swap"; \
	fi

proof-private-orderflow:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "orderflow or relay"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "orderflow or relay"; \
	fi
	curl -sf http://127.0.0.1:8000/metrics | grep -i relay

proof-stealth-triggers:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "stealth and triggers"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "stealth and triggers"; \
	fi
	docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml logs --tail=200 mev-bot | grep -i stealth

proof-stealth-e2e:
	PYTHONPATH=. $(PYTHON_CMD) scripts/stealth_e2e.py

proof-sniper-sandwich:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "sniper or sandwich"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "sniper or sandwich"; \
	fi
	PYTHONPATH=. $(PYTHON_CMD) scripts/detector_eval.py

proof-backrun-calculator:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "backrun and calculator"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "backrun and calculator"; \
	fi

proof-bundle-builder:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "bundle"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "bundle"; \
	fi

proof-builder-submissions:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "builder and submit"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "builder and submit"; \
	fi
	curl -sf http://127.0.0.1:8000/metrics | grep -i builder

proof-hunter-e2e:
	PYTHONPATH=. $(PYTHON_CMD) scripts/hunter_e2e.py
	PYTHONPATH=. $(PYTHON_CMD) scripts/pnl_check.py

proof-strategy-orchestrator:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "orchestrator"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "orchestrator"; \
	fi
	curl -sf http://127.0.0.1:8000/metrics | grep -i orchestrator

proof-adaptive-risk:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "adaptive and risk"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "adaptive and risk"; \
	fi

proof-kill-switch-api:
	$(MAKE) pause-test
	curl -sf -X POST http://127.0.0.1:8000/pause

proof-gas-policy:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "gas policy"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "gas policy"; \
	fi
	curl -sf http://127.0.0.1:8000/metrics | grep -i gas

proof-alerting:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "alert"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "alert"; \
	fi

proof-grafana-dashboards:
	cd docker && docker compose up -d grafana
	ls docker/grafana/dashboards

proof-nightly-etl-duckdb:
	PYTHONPATH=. $(PYTHON_CMD) scripts/nightly_etl.py
	PYTHONPATH=. $(PYTHON_CMD) scripts/weekly_report.py --json

proof-weekly-analytics-report:
	PYTHONPATH=. $(PYTHON_CMD) scripts/weekly_report.py

proof-secrets-hardening:
	grep -R -n "PRIVATE_KEY\|TRADER_PRIVATE_KEY" . || true
	PYTHONPATH=. $(PYTHON_CMD) scripts/secret_scan.py

proof-access-control:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "auth or rate_limit or allowlist"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "auth or rate_limit or allowlist"; \
	fi

proof-audit-logging:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "audit"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "audit"; \
	fi

proof-pre-submit-simulation:
	@if [ -x "$(VENV_BIN)/pytest" ]; then \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(VENV_BIN)/pytest -q -k "pre_submit or simulation"; \
	else \
		PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -k "pre_submit or simulation"; \
	fi

# BUILD_BOARD

Proof-gated task board.
Rules:
- Move tasks across columns only when proof commands pass.
- Do not mark any task `Done` without proof.

## Columns
- `Done`
- `Ready`
- `In Progress`
- `Backlog`

## Board

| Task | Column | Acceptance Criteria | Proof Commands | Artifact Paths |
|---|---|---|---|---|
| Bootstrap repo & compose | Done | Repo scaffold exists; docker compose up works; health endpoint returns 200 | `make proof-bootstrap` | `docker/docker-compose.yml`, `docker/docker-compose.override.yml`, `README.md` |
| Runtime status command (`scripts/status.py` + `make status`) | Done | Prints git hash, python version, env mode, docker compose health, DB connection, latest migration, paused flag, and latest smoke output timestamps/paths | `make status` | `scripts/status.py`, `Makefile` |
| Typed config loader | Done | Pydantic settings loaded; .env validated at startup; missing vars fail startup | `make proof-typed-config` | `bot/config.py`, `.env.runtime`, `tests/` |
| Postgres + migrations | Done | Tables trades/opportunities/pnl_daily/alerts exist; migrations run idempotently; migration command documented | `make proof-postgres-migrations` | `scripts/migrate.py`, `migrations/`, `sql/migrations/`, `docs/` |
| Telemetry baseline | Done | Prometheus metrics exposed; Grafana shows sample dashboard; scrape target passes health check | `make proof-telemetry-baseline` | `bot/core/telemetry.py`, `docker/prometheus/prometheus.yml`, `docker/grafana/` |
| GAP-1: mempool-producer wiring | Done | `mempool-producer` must run WS publisher path, not consumer module | `make gap1-proof` | `docker/docker-compose.override.yml`, `Makefile` |
| GAP-2: WS->Redis publisher runtime path | Done | Compose stack starts; Redis stream grows over time; latest stream entries are readable | `make gap2-proof` | `docker/docker-compose.override.yml`, `scripts/ws_to_redis.py`, `Makefile` |
| GAP-3: consumer RPC env mismatch | Done | Consumer accepts `RPC_HTTP` fallback from `RPC_ENDPOINT_PRIMARY`; consumer fetch success appears in logs | `make gap3-proof` | `bot/workers/mempool_consumer.py`, `docker/docker-compose.override.yml`, `Makefile` |
| Mempool monitor (multi-WS) | Done | >=100 pending tx/min on Polygon test; unit tests cover reconnect/race; graceful backoff on WS errors | `make proof-mempool-monitor` | `bot/workers/mempool_consumer.py`, `bot/mempool/`, `docker/docker-compose.override.yml`, `tests/`, `artifacts/proof/mempool_rate.json` |
| WS->Redis publisher path + consumer handoff wiring | Done | Producer runs real WS publisher (`ws_to_redis`); stream entries are fresh and growing; entries contain `hash/tx/ts/ts_ms`; consumer group is attached with non-error lag | `make proof-ws-redis-handoff` | `docker/docker-compose.override.yml`, `scripts/ws_to_redis.py`, `bot/workers/mempool_consumer.py`, `scripts/validate_ws_redis_handoff.py`, `Makefile` |
| Permit2 handler | Done | Off-chain signature generation works; nonce management persisted; unit tests cover domain/expiry | `make proof-permit2` | `bot/permit2/`, `tests/` |
| Exact-output swap | Done | Calldata builds; simulation passes; revert caught; slippage <= config | `make proof-exact-output-swap` | `bot/exec/`, `bot/sim/`, `tests/` |
| Private orderflow router | Done | Routes by tx traits; retry+fallback implemented; relay success metric recorded | `make proof-private-orderflow` | `bot/exec/orderflow.py`, `bot/core/telemetry.py`, `tests/` |
| Stealth triggers | Done | Flags driven by config; threshold unit tests pass; toggles reflected in logs | `make proof-stealth-triggers` | `bot/strategy/stealth.py`, `bot/config.py`, `tests/` |
| Stealth E2E | Done | >=10 trades via private path; zero sandwiched in test env; runbook updated | `make proof-stealth-e2e` | `scripts/stealth_e2e.py`, `RUNBOOK.md`, `logs/`, `artifacts/proof/stealth_e2e.json` |
| Sniper/sandwich detectors | Done | Heuristics pass fixtures; precision/recall logged; false-positive rate reported | `make proof-sniper-sandwich` | `bot/detectors/`, `tests/fixtures/`, `reports/detector_eval.json` |
| Backrun calculator | Done | Profitable path computed; simulation > 0 profit; output includes gas + tip | `make proof-backrun-calculator` | `bot/strategy/hunter.py`, `bot/sim/`, `tests/` |
| Bundle builder | Done | Atomic bundle built; replay protection in place; retry policy enforced | `make proof-bundle-builder` | `bot/builders/`, `tests/` |
| Builder submissions | Done | Submits to builder pool; success ratio metric emitted; exponential backoff on errors | `make proof-builder-submissions` | `bot/builders/submitter.py`, `bot/core/telemetry.py`, `tests/` |
| Hunter E2E | Ready | >=5 successful backruns in fork/testnet; positive P&L; artifacts saved | `make proof-hunter-e2e` | `scripts/hunter_e2e.py`, `scripts/pnl_check.py`, `artifacts/hunter/hunter_e2e.json` |
| Strategy orchestrator | Ready | Rule-based switch (stealth/hunter/hybrid) works; state visible in telemetry; manual override documented | `make proof-strategy-orchestrator` | `bot/orchestration/orchestrator.py`, `docs/`, `tests/` |
| AdaptiveRiskManager | In Progress | Kelly with safety factor applied; hard caps enforced; unit tests for edge cases | `make proof-adaptive-risk` | `bot/risk/adaptive.py`, `tests/` |
| Kill-switch & API | Done | /pause and /resume endpoints live; persisted flag survives restart; E2E verified | `make pause-test` and `make proof-kill-switch-api` | `bot/api/main.py`, `tests/integration/test_pause_api.py`, `Makefile`, `sql/migrations/0002_ops_state.sql` |
| Gas policy module | In Progress | Dynamic gas ceiling applied; alerts on exceed; metric exported | `make proof-gas-policy` | `bot/gas/`, `bot/core/telemetry.py`, `tests/` |
| Alerting | In Progress | Discord embeds on thresholds; test fixtures pass; throttle prevents spam | `make proof-alerting` | `bot/alerts/`, `tests/fixtures/`, `docker/alertmanager/` |
| Grafana dashboards | Backlog | Panels for P&L, win-rate, gas %, relay success, latency; JSON provisioning works | `make proof-grafana-dashboards` | `docker/grafana/dashboards/`, `docker/grafana/datasources/` |
| Nightly ETL -> DuckDB | Backlog | ETL job persists data; weekly report script outputs JSON; job exit code monitored | `make proof-nightly-etl-duckdb` | `scripts/nightly_etl.py`, `scripts/weekly_report.py`, `data/duckdb/nightly_etl.json` |
| Weekly analytics report | Backlog | Summary + recommendations saved to DB; report posted to Discord; run logged | `make proof-weekly-analytics-report` | `scripts/weekly_report.py`, `bot/analytics/`, `logs/weekly_report.log` |
| Secrets hardening | Backlog | Docker secrets/age vault used; no plaintext keys in env/logs; secret scan passes | `make proof-secrets-hardening` | `docker/secrets/`, `.env.runtime`, `scripts/secret_scan.py`, `artifacts/proof/secret_scan.json` |
| Access control | Backlog | IP allowlist + API key + rate limit enforced; 403 on breach; logs include requester | `make proof-access-control` | `bot/api/`, `bot/security/`, `tests/` |
| Audit logging | Backlog | Append-only audit captures decisions; includes mode/reason; tamper check passes | `make proof-audit-logging` | `bot/audit/`, `tests/`, `logs/` |
| Pre-submit simulation | Backlog | Every tx simulated pre-submit; failures block submit; metrics logged | `make proof-pre-submit-simulation` | `bot/sim/`, `bot/exec/`, `tests/` |

## Ongoing Requirement

From now on, every PR/change must update this board:
- Add or update task rows with proof commands and artifact paths.
- Move tasks across columns only when proof commands pass.
- Keep tasks out of `Done` until proof is recorded.

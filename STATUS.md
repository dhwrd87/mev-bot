# STATUS

Generated: 2026-02-28T05:03:48+00:00

Progress: 16/23 DONE

## Architecture (Inferred)
- Services: mev-bot, mempool-consumer, mempool-producer, redis, postgres, prometheus, grafana, alertmanager
- Core modules: bot/mempool, bot/workers, bot/exec, bot/orchestration, bot/storage, bot/telemetry
- Primary docs: docs/BUILD_BOARD.md, docs/TEST_MAP.md, docs/WIRING_MAP.md

## Sources
- docs/BUILD_BOARD.md
- docs/TEST_MAP.md
- docs/WIRING_MAP.md
- docs/ARCHITECTURE.md
- RUNBOOK.md
- docker/smoke.sh
- scripts/smoke_all.py

## Platform
- **Compose stack and health checks**
  - state: `DONE`
  - board column: `Done` (Bootstrap repo & compose)
  - evidence: `docker/docker-compose.yml:2`, `docker/docker-compose.yml:39`, `docker/docker-compose.yml:23`
  - next smallest change: Add a single `make up-validate` target that runs compose up and smoke in sequence.
- **Migration framework and linked SQL migrations**
  - state: `DONE`
  - board column: `Done` (Postgres + migrations)
  - evidence: `scripts/migrate.py`, `migrations/0102_mempool_pipeline_persistence.sql`, `migrations/0103_candidates_paper_mode.sql`, `migrations/0104_candidates_outcomes.sql`
  - next smallest change: Add `make migrate` alias to standardize migration invocation for operators.
- **Status report generation from script**
  - state: `DONE`
  - board column: `Done` (Runtime status command (`scripts/status.py` + `make status`))
  - evidence: `scripts/status.py`, `Makefile:130`
  - next smallest change: Wire STATUS generation into CI so docs/status drift is caught automatically.

## Mempool Pipeline
- **WS producer wired to redis stream**
  - state: `DONE`
  - board column: `Done` (WS->Redis publisher path + consumer handoff wiring)
  - evidence: `docker/docker-compose.yml:77`, `docker/docker-compose.yml:79`, `bot/mempool/ws_subscribe.py:122`
  - next smallest change: Add producer integration test that asserts stream writes from a mocked websocket.
- **Consumer persists stream events and tx/error tables**
  - state: `DONE`
  - board column: `Done` (Mempool monitor (multi-WS))
  - evidence: `bot/workers/mempool_consumer.py:20`, `bot/workers/mempool_consumer.py:21`, `bot/workers/mempool_consumer.py:22`
  - next smallest change: Add a unit test with fake Redis + fake RPC covering event/tx/error persistence branches.
- **DB debug stats endpoint for pipeline tables**
  - state: `DONE`
  - board column: `Done` (Telemetry baseline)
  - evidence: `bot/api/main.py:246`, `bot/api/main.py:252`
  - next smallest change: Expose DB stats in Prometheus metrics to enable alert thresholds.

## Detection
- **Paper candidate detector worker**
  - state: `DONE`
  - evidence: `bot/workers/candidate_detector.py`, `bot/workers/candidate_detector.py:86`, `bot/workers/candidate_detector.py:100`
  - next smallest change: Add small deterministic fixture test for allowlist and priority-fee candidate emissions.
- **Allowlist config file present**
  - state: `DONE`
  - evidence: `config/allowlist.json`
  - next smallest change: Populate non-empty allowlist per chain and validate addresses at load time.
- **Candidates API endpoint**
  - state: `DONE`
  - evidence: `bot/api/main.py:276`, `bot/api/main.py:280`
  - next smallest change: Add query params (`kind`, `limit`, `since`) to support targeted review workflows.
- **Paper evaluator outcomes worker**
  - state: `DONE`
  - evidence: `bot/workers/candidate_evaluator.py`, `bot/workers/candidate_evaluator.py:44`, `bot/workers/candidate_evaluator.py:25`
  - next smallest change: Run evaluator as a dedicated compose service and expose its health/status metric.

## Simulation
- **Simulator smoke path**
  - state: `TODO`
  - board column: `Backlog` (Pre-submit simulation)
  - evidence: `scripts/sim_smoke.py`, `Makefile:123`
  - next smallest change: Add a CI job that runs `make sim-smoke` on every PR.
- **Pre-submit simulation integration**
  - state: `TODO`
  - board column: `Backlog` (Pre-submit simulation)
  - evidence: `bot/sim/pre_submit.py`, `docs/BUILD_BOARD.md:49`
  - next smallest change: Hook pre-submit simulation into all execution paths and mark board item DONE with proof.

## Execution
- **Private orderflow router module**
  - state: `DONE`
  - board column: `Done` (Private orderflow router)
  - evidence: `bot/exec/orderflow.py`, `bot/exec/orderflow.py:18`
  - next smallest change: Add regression tests for timeout + fallback ordering between relay endpoints.
- **Stealth execution flow**
  - state: `DONE`
  - board column: `Done` (Stealth E2E)
  - evidence: `bot/strategy/stealth.py`, `scripts/stealth_e2e.py`
  - next smallest change: Automate `stealth_e2e` artifact capture in CI nightly runs.
- **Hunter execution flow**
  - state: `PARTIAL`
  - board column: `Ready` (Hunter E2E)
  - evidence: `bot/hunter/runner.py`, `scripts/hunter_e2e.py`, `docs/BUILD_BOARD.md:37`
  - next smallest change: Produce one reproducible proof artifact from `scripts/hunter_e2e.py` and move board row to Done.

## PnL/Accounting
- **Trades and daily pnl schema**
  - state: `DONE`
  - board column: `Done` (Postgres + migrations)
  - evidence: `sql/migrations/0001_init.sql:7`, `sql/migrations/0001_init.sql:34`
  - next smallest change: Add write path from execution results to `pnl_daily` rollups.
- **Nightly ETL/report scripts**
  - state: `TODO`
  - board column: `Backlog` (Nightly ETL -> DuckDB)
  - evidence: `scripts/nightly_etl.py`, `scripts/weekly_report.py`, `docs/BUILD_BOARD.md:44`
  - next smallest change: Add one cron-compatible entrypoint and proof artifact path for nightly ETL outputs.

## Observability
- **Prometheus + Grafana stack**
  - state: `TODO`
  - board column: `Backlog` (Grafana dashboards)
  - evidence: `docker/docker-compose.yml:136`, `docker/docker-compose.yml:155`, `bot/core/telemetry.py`
  - next smallest change: Add dashboard coverage for paper-mode candidate and outcome metrics.
- **Paper report endpoint**
  - state: `DONE`
  - evidence: `bot/api/main.py:305`, `bot/api/main.py:334`
  - next smallest change: Add p50/p95 inclusion delay and outcome counts by candidate kind.
- **Smoke coverage for pipeline and paper endpoints**
  - state: `DONE`
  - evidence: `smoke.sh:84`, `smoke.sh:110`
  - next smallest change: Add smoke checks for evaluator worker process health and non-zero evaluated outcomes.

## Safety/Risk
- **Pause/resume kill switch API**
  - state: `DONE`
  - board column: `Done` (Kill-switch & API)
  - evidence: `bot/api/main.py:351`, `bot/api/main.py:361`, `tests/integration/test_pause_api.py`
  - next smallest change: Add authorization layer for pause/resume endpoints.
- **Adaptive risk manager module**
  - state: `PARTIAL`
  - board column: `In Progress` (AdaptiveRiskManager)
  - evidence: `bot/risk/adaptive.py`, `docs/BUILD_BOARD.md:39`
  - next smallest change: Implement and test hard-cap enforcement path, then close remaining board acceptance criteria.
- **Secrets hardening / access control**
  - state: `TODO`
  - board column: `Backlog` (Access control)
  - evidence: `scripts/secret_scan.py`, `bot/security`, `docs/BUILD_BOARD.md:46`, `docs/BUILD_BOARD.md:47`
  - next smallest change: Add API key middleware and deny-by-default allowlist check with one integration test.

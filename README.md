Fork mode (later):
1) export POLYGON_RPC=https://polygon-mainnet.g.alchemy.com/v2/<KEY>
2) anvil --fork-url $POLYGON_RPC --port 8545
3) pytest -k fork_skeleton -q

## Python + Reproducible Setup

- Recommended Python version: **3.12**
- One-command local setup:
  - `./scripts/dev_setup.sh`
- This creates `.venv` and installs pinned runtime/dev dependencies from:
  - `requirements.txt`
  - `requirements-dev.txt`
- Run tests:
  - `./scripts/test.sh`
  - collection only: `./scripts/test.sh --collect-only`
- If local pip access is restricted, run tests in Docker:
  - `./scripts/test_docker.sh`
  - collection only: `./scripts/collect_docker.sh`
  - full build + unit + integration validation: `./scripts/validate_docker.sh`
  - include API stack service when needed: `WITH_MEV_BOT=1 ./scripts/validate_docker.sh`

## Docker Dependency Install

- Runtime image installs only runtime deps from `requirements.txt` via `docker/Dockerfile.core`.
- Dev/test deps remain local-only by default (`requirements-dev.txt`) to keep the runtime image smaller.
- To build an image with test deps too:
  - `docker build -f docker/Dockerfile.core --build-arg INSTALL_DEV=true -t local/mev-bot:test .`

## Prometheus Metrics Exporter

- Exporter uses `prometheus_client` and starts on:
  - `METRICS_PORT` (default `9100`)
- Endpoint:
  - `http://127.0.0.1:${METRICS_PORT:-9100}/metrics`
- Quick check:
  - `curl -s http://127.0.0.1:9100/metrics | head`

### Standardized bot metrics (`mevbot_` prefix)
- Outcomes:
  - `mevbot_pnl_realized_usd` (gauge)
  - `mevbot_fees_total_usd` (gauge snapshot)
  - `mevbot_drawdown_usd` (gauge)
- Pipeline:
  - `mevbot_opportunities_seen_total`
  - `mevbot_opportunities_attempted_total`
  - `mevbot_opportunities_filled_total`
- Execution:
  - `mevbot_tx_sent_total`
  - `mevbot_tx_confirmed_total`
  - `mevbot_tx_failed_total`
  - `mevbot_tx_confirm_latency_seconds` (histogram)
  - `mevbot_sim_fail_total`
  - `mevbot_tx_revert_total`
- RPC/Health:
  - `mevbot_rpc_latency_seconds` (histogram)
  - `mevbot_rpc_errors_total{provider,code_bucket}`
  - `mevbot_head_lag_blocks`
  - `mevbot_slot_lag`
  - `mevbot_state` (enum gauge: `UNKNOWN=0, PAUSED=1, READY=2, TRADING=3, DEGRADED=4, PANIC=5`)

Example:
```bash
curl -s http://127.0.0.1:9100/metrics | rg '^mevbot_(state|tx_sent_total|rpc_latency_seconds|rpc_errors_total)'
```

### Verify Prometheus scrape
1. Restart stack:
   - `docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime up -d --build`
2. Run automated verification:
   - `./scripts/verify_metrics.sh`
3. Optional: use a non-default Prometheus URL:
   - `PROM_URL=http://127.0.0.1:9090 ./scripts/verify_metrics.sh`
4. Expected:
   - `mev-bot` scrape target is `UP`
   - optional jobs (`node-exporter`, `cadvisor`, `postgres-exporter`, `redis-exporter`) print `SKIP` if not configured
   - key bot metrics are present via Prometheus query API

### Verify End-to-End Data Flow
Run:
- `./scripts/verify_data_flow.sh`

This checks blockchain -> bot -> metrics end-to-end by validating:
- `mevbot_heartbeat_ts` is increasing
- chain head/slot metric advances
- synthetic stream events cause seen counters to increase

Troubleshooting:
- `docs/troubleshooting_metrics.md`

## Grafana Dashboards (Git Managed)

Dashboards are now managed by git; do not edit in UI except for temporary debugging.

## Quickstart on Debian/Raspberry Pi (PEP 668)

1) Create the project virtual environment and install all dependencies (runtime + dev):
   `make venv`
2) Run tests from the project venv:
   `make test`
3) Run the golden-path smoke script:
   `make smoke`

Testing
- Use `make test` or `make test-verbose`.
- Default pytest run excludes integration tests (`-m "not integration"`).
- Run integration tests explicitly with `-m integration` (stack/services required).
- Tests must run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and `-p pytest_asyncio` to keep plugin loading deterministic and avoid third-party plugins altering async test behavior.

Prometheus Multiprocess Mode
- Default runtime should use single-process metrics (do not set `PROMETHEUS_MULTIPROC_DIR`).
- Enable `PROMETHEUS_MULTIPROC_DIR` only for true multi-worker setups (for example, Gunicorn/Uvicorn workers > 1).
- On startup, the bot now validates multiprocess shard files and, if corrupt, logs a warning, resets the shard directory, and continues startup.

How to switch chains
- Edit `.env.runtime` and set `CHAIN=<sepolia|amoy|mainnet|polygon>`.
- Optionally set `CHAIN_ID` if you need a non-default chain id.

## Chain Profiles (YAML Templates)

Canonical profile folders:
- `config/chains/evm/`
- `config/chains/sol/`
- shared DEX defaults: `config/dex_packs/`

Runtime selector:
- `CHAIN_PROFILE=<profile-name>` (example: `sepolia-testnet`, `base-mainnet`, `solana-devnet`)

Print resolved profile (defaults merged + validated):
```bash
python3 scripts/print_chain_profile.py sepolia-testnet
# or rely on env
CHAIN_PROFILE=base-mainnet python3 scripts/print_chain_profile.py
```

### Add a New EVM Chain in 3 Steps
1. Add chain profile YAML:
   - `config/chains/evm/<chain>-<network>.yaml`
   - include required keys: `family`, `chain`, `network`, `rpc.endpoints`, `explorer`, `native_asset`, `risk`, `dexes_enabled`, `dex_configs`.
   - `risk` keys: `max_fee_gwei`, `slippage_bps`, `max_daily_loss_usd`, `min_edge_bps`.
2. Define/override DEX configs:
   - use `type: evm_univ2` and/or `type: evm_univ3` under `dex_configs`.
   - shared defaults come from `config/dex_packs/evm_univ2.yaml` and `config/dex_packs/evm_univ3.yaml`.
3. Validate:
   - `python3 scripts/print_chain_profile.py <profile-name>`
   - `python3 -m pytest -q tests/test_chain_profile_loader.py`

Paper Mode (Candidate Detector)
- Purpose: detect and persist opportunities only. No signing, no sending, no bundle submission.
- Migration:
  - `cd docker && docker compose exec -T mev-bot python3 scripts/migrate.py --one 0103_candidates_paper_mode.sql`
- Run detector worker:
  - `PYTHONPATH=. python3 -m bot.workers.candidate_detector`
- Run evaluator worker:
  - `PYTHONPATH=. python3 -m bot.workers.candidate_evaluator`
- Config:
  - Allowlist file: `config/allowlist.json` with JSON shape `{"contracts":["0x..."]}`
  - Env knobs:
    - `CANDIDATE_POLL_SEC` (default `5`)
    - `CANDIDATE_BATCH_LIMIT` (default `1000`)
    - `CANDIDATE_PRIORITY_FEE_WEI` (default `2000000000`)
    - `CANDIDATE_ALLOWLIST_PATH` (default `config/allowlist.json`)
    - `CANDIDATE_START_TS_MS` (default `0`)
    - `EVAL_POLL_MS` (default `2000`)
    - `EVAL_TIMEOUT_S` (default `900`)
- API:
  - `GET /candidates` returns last 50 candidates.
  - `GET /paper_report` returns last-24h paper evaluation summary.

## Discord Operator UI (separate process)

This is a standalone operator bot and does not modify trading code paths.

### Required env vars (`.env.runtime`)
- `DISCORD_OPERATOR_TOKEN`
- `DISCORD_OPERATOR_COMMAND_CHANNEL_ID`
- `DISCORD_OPERATOR_AUDIT_CHANNEL_ID`
- `DISCORD_OPERATOR_STATUS_CHANNEL_ID`
- Optional:
  - `DISCORD_OPERATOR_STATUS_REFRESH_S` (default `45`)
  - `METRICS_SCRAPE_URL` (optional; if unset, metrics fields show `—`)
  - `OPERATOR_STATE_FILE` (default `ops/operator_state.json`)

### Run locally
```bash
python -m ops.discord_operator
```

### Run in docker (example)
```bash
docker compose run --rm \
  -e DISCORD_OPERATOR_TOKEN \
  -e DISCORD_OPERATOR_COMMAND_CHANNEL_ID \
  -e DISCORD_OPERATOR_AUDIT_CHANNEL_ID \
  -e DISCORD_OPERATOR_STATUS_CHANNEL_ID \
  -e DISCORD_OPERATOR_STATUS_REFRESH_S=45 \
  mev-bot python -m ops.discord_operator
```

Using the compose service:
```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime up -d operator
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime logs -f operator
```

### Commands (command channel only)
- `!help`
- `!status`
- `!pause`
- `!resume`
- `!kill on|off`
- `!mode dryrun|paper|live`
- `!chain set <name>`
- `!config`
- `!ping`

Every command action is audited to the audit channel with UTC timestamp, actor, command/args, and success/failure result.
`!chain set <name>` also forces operator state to `PAUSED`; manual `!resume` is required before trading can continue.

### Status card content
The pinned status card refreshes every `DISCORD_OPERATOR_STATUS_REFRESH_S` seconds and recreates itself if deleted. It shows:
- Bot State
- Mode
- Active Chain
- Health (heartbeat, uptime, error counters)
- Latest Trade (if available)
- Metrics quick read (RPC latency p95, trades sent/failed in last 10m if available)

Unavailable values are rendered as `—`.

### `operator_state.json` schema
Stored at `ops/operator_state.json` by default (atomic replace writes), for future bot integration:

```json
{
  "state": "PAUSED|READY|TRADING|DEGRADED|PANIC|UNKNOWN",
  "mode": "dryrun|paper|live|UNKNOWN",
  "kill_switch": false,
  "chain_target": "EVM:base|SOL:solana|UNKNOWN",
  "last_updated": "2026-02-28T12:34:56.123456+00:00",
  "last_actor": "123456789012345678:operator-name"
}
```

### Trading Control Contract (enforced at broadcast path)
- Broadcast guard reads `OPERATOR_STATE_PATH` (default `ops/operator_state.json`) before sending transactions.
- Send is blocked when:
  - `kill_switch == true`, or
  - `state != "TRADING"`
- Blocked sends return `blocked_by_operator` and do not call the RPC/relay send function.
- Guard logs structured fields (`state`, `mode`, `last_actor`) and increments `blocked_by_operator_total`.
- State file reads are cached for up to 1 second for low overhead.
- Chain target switching:
  - Operator sets `chain_target` via `!chain set <name>` and forces `PAUSED`.
  - Runtime monitor detects target change and applies `PAUSED -> SYNCING -> READY`, including RPC and wallet balance validation.
  - Trading remains blocked until operator state is `TRADING` and internal runtime state is `READY|TRADING`.

### Internal Runtime State + Invariants
- Runtime state machine states: `BOOTING|SYNCING|READY|TRADING|PAUSED|DEGRADED|PANIC` (see `bot/core/state.py`).
- Invariants module (`bot/core/invariants.py`) drives runtime degradation/panic:
  - `rpc p99` above threshold -> `DEGRADED`
  - drawdown above threshold -> `PANIC`
  - tx failure rate above threshold -> `DEGRADED`
  - operator kill-switch -> `PANIC`
  - operator state not `TRADING` -> `PAUSED`
- Tunables:
  - `INVAR_RPC_P99_MS_THRESHOLD` (default `1500`)
  - `INVAR_RPC_P99_WINDOW_MIN` (default `5`)
  - `INVAR_DRAWDOWN_THRESHOLD` (default `0.20`)
  - `INVAR_TX_FAIL_RATE_THRESHOLD` (default `0.50`)
  - `INVAR_TX_FAIL_RATE_WINDOW_MIN` (default `5`)
  - `INVAR_MONITOR_INTERVAL_S` (default `15`)
- Health snapshot for operator status card:
- `HEALTH_SNAPSHOT_PATH` (default `ops/health_snapshot.json`)
- `HEALTH_SNAPSHOT_INTERVAL_S` (default `10`)

Health snapshot schema (written atomically every interval with temp file + rename):
- `ts`, `family`, `chain`, `state`, `mode`
- `head`, `slot`, `lag`, `last_trade_ts`
- `trades_sent_10m`, `trades_failed_10m`
- `rpc_p95_ms`, `rpc_p99_ms`
- `pnl_today_usd`, `drawdown_usd`, `fees_today_usd`

## Alertmanager -> Discord via Operator Alert Router

Optional path (existing webhook relay remains enabled in parallel until verified).

### Service
- FastAPI router: `ops/alert_router.py`
- Endpoint: `POST /alertmanager`
- Dedupe: fingerprint-based, 5-minute TTL
- Target channel:
  - `DISCORD_OPERATOR_ALERTS_CHANNEL_ID` (preferred)
  - fallback `DISCORD_OPERATOR_AUDIT_CHANNEL_ID`

### Env vars
- `DISCORD_OPERATOR_TOKEN`
- `DISCORD_OPERATOR_ALERTS_CHANNEL_ID` (optional, recommended)
- `DISCORD_OPERATOR_AUDIT_CHANNEL_ID` (fallback channel)

### Compose
Start router:
```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime up -d alert-router
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime logs -f alert-router
```

### Alertmanager config snippet
```yaml
receivers:
  - name: discord-relay
    webhook_configs:
      - url: http://alert-webhook-relay:8080/webhook
        send_resolved: true
      - url: http://alert-router:8090/alertmanager
        send_resolved: true
```

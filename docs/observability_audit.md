# Observability Audit Report

Date: 2026-02-28  
Scope: Grafana dashboards, Prometheus scrape/alerts wiring, bot metrics exposure and data quality.

## 1) Grafana Dashboard Inventory

### 1.1 Currently loaded dashboards (Grafana API)
Source: `GET http://127.0.0.1:3000/api/search?type=dash-db`

| Title | UID | Folder |
|---|---|---|
| Execution Deep Dive | `execution-deep-dive` | root (`null`) |
| MEV Bot Overview | `6c18600d-656c-4598-a270-06b02ebbb6a8` | root (`null`) |
| MEV Bot – Starter | `mev-bot-starter` | root (`null`) |
| MEV Bot • Mempool Monitor | `0bf3b825-fda7-41a5-9c67-86ceb9b2d8ba` | root (`null`) |
| Operator Overview | `operator-overview` | root (`null`) |
| Private Orderflow Health | `private-orderflow` | root (`null`) |
| Strategy Analytics | `strategy-analytics` | root (`null`) |

Note: provisioning defines folder `"MEV Bot"` in [dashboards.yml](/Users/user1/stacks/mev/mev-bot/grafana/provisioning/dashboards/dashboards.yml:5), but loaded dashboards are currently in root.

### 1.2 Dashboard files present in repo
Active mount path (compose): [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:193) and [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:194)  
Loaded files come from `grafana/dashboards/*.json`.

- Active set:
  - [execution_deep_dive.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/execution_deep_dive.json)
  - [mempool.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mempool.json)
  - [mev-bot-dashboard.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-dashboard.json)
  - [mev-bot-overview.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-overview.json)
  - [operator_overview.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/operator_overview.json)
  - [private_orderflow.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/private_orderflow.json)
  - [strategy_analytics.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/strategy_analytics.json)

- Legacy/duplicate tree (not mounted by current compose):
  - `docker/grafana/dashboards/*.json`
  - includes duplicate `mev-bot-dashboard copy.json`

### 1.3 Dashboards likely test/unused/stale
- `docker/grafana/dashboards/*`: likely unused in current stack (compose mounts `../grafana/*`, not `docker/grafana/*`).
- `grafana/dashboards/mev-bot-dashboard.json` (Starter): 0/9 panel queries with any series currently.
- `grafana/dashboards/private_orderflow.json`: 0/6 panel queries with any series currently.
- `grafana/dashboards/operator_overview.json`: 0/7 panel queries with any series currently.
- `grafana/dashboards/strategy_analytics.json`: 0/8 panel queries with any series currently.

## 2) Prometheus Scrape Targets and Status

Source: `GET http://127.0.0.1:9090/api/v1/targets`

| Job | Target | Scrape URL | Health |
|---|---|---|---|
| `prometheus` | `localhost:9090` | `http://localhost:9090/metrics` | `up` |
| `mev-bot` | `mev-bot:9100` | `http://mev-bot:9100/metrics` | `up` |
| `alertmanager` | `mev-alertmanager:9093` | `http://mev-alertmanager:9093/metrics` | `up` |

Prometheus config source confirms these jobs: [prometheus.yml](/Users/user1/stacks/mev/mev-bot/docker/prometheus/prometheus.yml:9)

## 3) Metrics Exposed by the Bot

### 3.1 Exposure paths
- Dedicated exporter scrape target: `mev-bot:9100/metrics` (Prometheus job `mev-bot`)
- API also exposes `/metrics`: [main.py](/Users/user1/stacks/mev/mev-bot/bot/api/main.py:30)
- Metrics endpoint uses multiprocess collector when `PROMETHEUS_MULTIPROC_DIR` is set: [metrics.py](/Users/user1/stacks/mev/mev-bot/bot/api/metrics.py:7)

### 3.2 Metric families discovered
- 104 metric families advertised via `# HELP/# TYPE` in sampled `/metrics`.
- 37 families currently have actual emitted series (sampled twice, 12s apart).

### 3.3 Active families (observed with series), labels, and change over time
Sample window: two snapshots, 12s apart.

| Metric family | Labels | Changed in sample |
|---|---|---|
| `bot_state` | `chain,chain_family,state` | no |
| `mevbot_bot_state` | `state` | no |
| `mevbot_dex_tx_detected_total` | `-` | no |
| `mevbot_mempool_message_latency_ms_*` | `le` (bucket only) | no |
| `mevbot_mempool_stream_consume_lag_ms_*` | `le` (bucket only) | no |
| `mevbot_mempool_tpm` | `-` | no |
| `mevbot_mempool_tps` | `-` | no |
| `mevbot_mempool_unique_tx_total` | `-` | no |
| `mevbot_orchestrator_decisions_total` | `mode,reason` | no |
| `mevbot_relay_fail_total` | `chain,reason,relay` | no |
| `mevbot_rpc_429_ratio` | `-` | no |
| `mevbot_rpc_circuit_breaker_open` | `-` | no |
| `mevbot_rpc_circuit_breaker_trips_total` | `-` | no |
| `mevbot_rpc_gettx_429_total` | `-` | no |
| `mevbot_rpc_gettx_errors_total` | `-` | no |
| `mevbot_rpc_gettx_ok_total` | `-` | no |
| `mevbot_rpc_rate_limit_waits_total` | `-` | no |
| `mevbot_sim_bundle_success_total` | `-` | no |
| `mevbot_sim_bundle_total` | `-` | no |
| `mevbot_sim_single_success_total` | `-` | no |
| `mevbot_sim_single_total` | `-` | no |
| `mevbot_stealth_decisions_total` | `decision` | no |
| `mevbot_stealth_flags_count` | `-` | no |
| `process_*`, `python_*` | varies | yes (`process_cpu_seconds_total`, `process_resident_memory_bytes` only) |

Observation: almost all bot/business metrics are static (mostly zero) during the sample; only process/runtime metrics moved.

## 4) Bad Data Causes (Root Causes + Evidence)

### 4.1 Multiprocess metrics file collision across containers (major)
All services mount the same `../tmp/prom_mp` and set the same `PROMETHEUS_MULTIPROC_DIR`:
- [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:15)
- [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:56)
- [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:85)
- [docker-compose.yml](/Users/user1/stacks/mev/mev-bot/docker/docker-compose.yml:117)

Observed shard files in container are only PID-1 names (`counter_1.db`, `gauge_all_1.db`, `histogram_1.db`), indicating cross-container collisions/overwrites in shared multiprocess directory.

Impact:
- Scraped metrics under-represent worker activity.
- Many dashboard panels show zero/no data despite active workers.

### 4.2 Invalid PromQL in dashboard
- [mempool.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mempool.json:26):
  - `mempool_ws_connected = on(endpoint) (mevbot_mempool_ws_connected)`
  - Prometheus error: `parse error: unexpected "="`

### 4.3 Panels referencing missing or mismatched metric names
- [mev-bot-dashboard.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-dashboard.json:14) references `mevbot_win_rate` (not exposed).
- [mev-bot-dashboard.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-dashboard.json:22) references `mevbot_profit_usd_total` (not exposed).
- [mev-bot-dashboard.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-dashboard.json:38) references `mevbot_kill_switch` (not exposed).
- [mev-bot-overview.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-overview.json:28) typo `mepool_ws_connected` (missing).
- [execution_deep_dive.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/execution_deep_dive.json:52) references `mevbot_rpc_errors_total` (not exposed; available is `mevbot_rpc_gettx_errors_total`).
- [strategy_analytics.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/strategy_analytics.json:64) `mevbot_edge_bps_bucket` missing.
- [strategy_analytics.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/strategy_analytics.json:76) `mevbot_slippage_bps_bucket` missing.

### 4.4 Label-schema inconsistency across metric families
Two families for similar concepts use different label keys:
- `bot.core.telemetry` uses `chain_family`/`chain`, e.g. [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:248)
- `ops.metrics` uses `family`/`chain`, e.g. [ops/metrics.py](/Users/user1/stacks/mev/mev-bot/ops/metrics.py:19)

Impact:
- Dashboard queries need OR/fallback logic and can silently return empty vectors if wrong family is queried.

### 4.5 Potential high-cardinality labels
- `trades_failed_total{reason=...}` reason is raw string from runtime paths: [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:253), [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:324)
- `rpc_latency_seconds{endpoint,method,...}` includes endpoint/method labels: [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:263), [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:338)
- `orderflow_submit_*{endpoint,method,chain}`: [telemetry.py](/Users/user1/stacks/mev/mev-bot/bot/core/telemetry.py:147)

### 4.6 PromQL misuse / fragile ratio logic
- [private_orderflow.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/private_orderflow.json:45):
  - `(increase(success[10m])) / clamp_max(increase(attempts[10m]), 1e9)`
  - `clamp_max` does not protect against near-zero denominator; `clamp_min(...,1)` is safer.

## 5) KEEP / DELETE-ARCHIVE / FIX / MISSING

## KEEP
- `MEV Bot • Mempool Monitor` (after query fix)
- `Execution Deep Dive` (after metric-name fixes + data wiring)
- `Operator Overview` (after data wiring)
- `Strategy Analytics` (after missing metrics implemented)
- `Private Orderflow Health` (if orderflow path is actively used; otherwise archive)

## DELETE / ARCHIVE
- Legacy tree `docker/grafana/dashboards/*` (not active mount path).
- `docker/grafana/dashboards/mev-bot-dashboard copy.json` (duplicate).
- `grafana/dashboards/mev-bot-dashboard.json` (starter dashboard mostly references nonexistent metrics; archive unless revived with current metric names).

## FIX (concrete)
1. Fix invalid query in [mempool.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mempool.json:26): use `mevbot_mempool_ws_connected`.
2. Fix typo in [mev-bot-overview.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/mev-bot-overview.json:28): `mepool_ws_connected` -> `mevbot_mempool_ws_connected`.
3. Replace missing `mevbot_rpc_errors_total` query in [execution_deep_dive.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/execution_deep_dive.json:52) with existing error counters (`mevbot_rpc_gettx_errors_total`, `mevbot_orderflow_submit_fail_total`, `mevbot_private_submit_errors_total`).
4. Standardize dashboard label filters to one schema (`family` vs `chain_family`) or add recording rules to unify.
5. Replace `clamp_max` ratio denominator in [private_orderflow.json](/Users/user1/stacks/mev/mev-bot/grafana/dashboards/private_orderflow.json:45) with `clamp_min(...,1)`.
6. Most important: stop sharing one multiprocess directory across containers.
   - either:
   - per-service multiprocess dir and explicit aggregation strategy, or
   - single-writer exporter model and workers push via Redis/OTLP/Pushgateway-like pattern.

## MISSING (for usability)
- `mevbot_edge_bps_bucket` histogram (needed by Strategy Analytics).
- `mevbot_slippage_bps_bucket` histogram (needed by Strategy Analytics).
- Current kill-switch gauge metric (or map to state metric) for operator dashboard.
- Consistent “today pnl” metric semantics (counter vs gauge with reset convention).
- Explicit “last_trade_timestamp_seconds” metric for status card and operator panels.

## Recommended Next PRs (small, additive)

PR-A (wiring correctness):
1. Fix invalid/typo queries in `mempool.json` + `mev-bot-overview.json`.
2. Update execution dashboard missing metric names.

PR-B (metric transport correctness):
1. Remove cross-container shared `PROMETHEUS_MULTIPROC_DIR`.
2. Implement one deterministic metrics aggregation model.
3. Verify non-process metrics change over a 5–10 minute window.

PR-C (schema consistency):
1. Standardize label keys (`family` vs `chain_family`) or add recording rules.
2. Update dashboards to single schema.

PR-D (strategy usability):
1. Add `edge_bps` and `slippage_bps` histograms.
2. Wire funnel and PnL strategy metrics so Strategy Analytics panels populate.

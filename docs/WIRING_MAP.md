# Wiring Map

## Runtime Entrypoints

### Docker Compose Services
- `mev-bot` → `uvicorn bot.api.main:app` (FastAPI + /metrics) in `docker/docker-compose.yml:2-43`.
- `mempool-consumer` → `python3 -m bot.workers.mempool_consumer` in `docker/docker-compose.yml:45-75`.
- `mempool-producer` → currently also `python -m bot.workers.mempool_consumer` (likely miswired) in `docker/docker-compose.yml:78-85`.
- `redis`, `postgres`, `prometheus`, `grafana`, `alertmanager` as infra in `docker/docker-compose.yml:103-182`.

### Python Module Entrypoints
- API server: `bot/api/main.py` (FastAPI app, startup hooks, mempool monitor wiring).
- Mempool consumer worker: `bot/workers/mempool_consumer.py` (`python -m bot.workers.mempool_consumer`).
- Golden path smoke: `bot/smoke/golden_path.py` (invoked by `scripts/golden_path_smoke.py`).

### Scripts / CLI Entrypoints
- `scripts/golden_path_smoke.py` → `bot.smoke.golden_path.main` (golden path smoke).
- `scripts/sim_smoke.py` → sim provider smoke.
- `scripts/smoke_orchestrator.py`, `scripts/smoke_stealth.py`, `scripts/smoke_all.py` (manual smoke runners).
- `scripts/ws_to_redis.py`, `scripts/ws_to_redis_min.py`, `scripts/diag_ws.py` (WS diagnostics + stream publisher).
- `scripts/send_via_router.py` (orderflow submission test).
- `scripts/redis_bootstrap.py` (stream/group bootstrap).
- `scripts/migrate.py` (DB migrations script).
- `bot/scripts/run_stealth_trade.py` (standalone stealth trade runner).
- `bot/scripts/smoke_exact_output.py` (exact-output swap smoke).

## Data Flow (Runtime)

```
WS Mempool (RPC/WebSocket)
   │
   │  (optional) WSMempoolMonitor
   ▼
Redis Stream (mempool:pending:txs)
   │
   │  bot/workers/mempool_consumer.py
   ▼
eth_getTransactionByHash (RPC_HTTP)
   │
   │  looks_like_dex() filter
   ▼
[TODO pipeline] -> Opportunity/Detector -> Orchestrator
   │
   ├─ Risk Manager (gate + sizing)
   ├─ Stealth Strategy → Orderflow Router → Private RPCs/Relays
   └─ Hunter Strategy → Bundle Builder → Builders
   │
   ▼
Receipts / Results
   │
   ├─ TradeRepo (Postgres)
   ├─ RiskRepo (Postgres)
   └─ AlertRepo (Discord webhook)
   │
   ▼
Prometheus metrics → Grafana dashboards → Alertmanager/webhooks
```

## Internal Module Boundaries (By Package)
- `bot/strategy/*` : Stealth + Hunter strategy logic, routing into executors/orderflow.
- `bot/exec/*` : Orderflow, bundle building, exact-output encoding, simulator.
- `bot/mempool/*` : WS subscription + detection helpers.
- `bot/hunter/*` : Backrun calculator + pipeline + executor.
- `bot/orchestration/*` : Orchestrator + adapters.
- `bot/ports/*` : Interfaces + real/fake adapters (repos, RPC, orderflow).
- `bot/storage/pg.py` : Postgres access.
- `bot/telemetry/*` : Metrics + alerts.


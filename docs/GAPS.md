# GAPS (Top 10 Linking Hell Issues)

1. **Mempool producer is miswired to the consumer module** — `mempool-producer` runs `python -m bot.workers.mempool_consumer`, so no WS ingestion happens and the stream is never populated. Ref: `docker/docker-compose.yml:78-85`.
2. **No producer service is actually wired to `WSMempoolMonitor`/WS→Redis scripts** — the intended publisher exists (`bot/mempool/monitor.py`, `scripts/ws_to_redis.py`) but none of the runtime services start it. Ref: `docker/docker-compose.yml:78-85`, `bot/mempool/monitor.py:20-83`.
3. **RPC env mismatch in mempool consumer** — consumer reads `RPC_HTTP`, but compose supplies `RPC_ENDPOINT_PRIMARY`; result: `fetch_tx` always returns `None` and DEX detection never runs. Ref: `bot/workers/mempool_consumer.py:22-28`, `docker/docker-compose.yml:45-65`.
4. **API WS endpoints are hardcoded to Polygon env vars** — `_ws_env_endpoints()` only uses `WS_POLYGON_*`, so non-Polygon chains never start a mempool monitor even when `CHAIN` differs. Ref: `bot/api/main.py:44-83`.
5. **Dead/invalid publisher hook in API** — `start_mempool_publisher()` references `K`, `aioredis`, `RpcClient`, and `ingest_to_queue` that are not imported or defined; calling it would crash. Ref: `bot/api/main.py:28-36`.
6. **Orderflow router relies on config that is not loaded at runtime** — `settings.chains[chain]` and `settings.routing.rules` are required; if config is missing, router init fails. Ref: `bot/exec/orderflow.py:155-170`, `bot/exec/orderflow.py:224-245`.
7. **Orderflow imports settings at import time** — any script importing `bot.exec.orderflow` triggers config validation and can fail when env vars are missing. Ref: `bot/exec/orderflow.py:10-11`, `bot/core/config.py`.
8. **Consumer-to-orchestrator handoff is a TODO** — DEX txs are detected but never forwarded to a pipeline/orchestrator. Ref: `bot/workers/mempool_consumer.py:134-137`.
9. **DB migrations are not invoked by compose/runtime** — migrations script exists, but services do not run it, so Postgres schema may be missing at runtime. Ref: `scripts/migrate.py:1-93`, `docker/docker-compose.yml:2-43`.
10. **Duplicate simulator class name creates import ambiguity** — two different `PreSubmitSimulator` classes exist under different modules. Ref: `bot/exec/simulator.py:38`, `bot/sim/pre_submit.py:21`.


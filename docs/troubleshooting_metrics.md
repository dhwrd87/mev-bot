# Metrics Data-Flow Troubleshooting

This runbook validates that data moves end-to-end:

`blockchain RPC -> bot runtime/workers -> /metrics -> Prometheus/Grafana`

## Quick verifier

Run:

```bash
./scripts/verify_data_flow.sh
```

The verifier checks:

1. `mevbot_heartbeat_ts` is increasing.
2. Chain progress is advancing:
   - EVM: `mevbot_chain_head`
   - SOL: `mevbot_chain_slot`
3. After synthetic Redis stream events, counters increase:
   - `mevbot_stream_events_observed_total` (runtime probe), or
   - `mevbot_opportunities_seen_total` or
   - `mevbot_candidate_pipeline_seen_total`

## Expected values

- `mevbot_heartbeat_ts` should move every monitor tick (usually every 10-15s).
- `mevbot_chain_head` or `mevbot_chain_slot` should increase over ~35s on healthy public networks.
- `mevbot_head_lag_blocks` / `mevbot_slot_lag` should normally stay near `0`.
- After verifier injects 3 test entries to `mempool:pending:txs`, at least one seen counter should rise.

## If heartbeat is stale

Check:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime logs --tail=200 mev-bot
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/metrics | rg '^mevbot_heartbeat_ts'
```

Common causes:

- runtime monitor loop crashed (look for `runtime monitor loop error`)
- bot process up but not executing startup hooks
- metrics endpoint serving stale process data

## If head/slot does not advance

Check:

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/metrics | rg '^mevbot_chain_(head|slot)|^mevbot_(head_lag_blocks|slot_lag)'
```

Common causes:

- selected RPC is unreachable or rate-limited
- chain has low activity / temporary halt (rare)
- wrong `CHAIN_FAMILY`/`CHAIN` config mismatch

## If counters do not increase after synthetic events

Check:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime logs --tail=200 candidate-pipeline
docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml --env-file .env.runtime exec -T redis redis-cli XLEN mempool:pending:txs
curl -sS http://127.0.0.1:8000/metrics | rg '^mevbot_(opportunities_seen_total|candidate_pipeline_seen_total)'
```

Common causes:

- candidate pipeline not running / consumer group blocked
- Redis stream mismatch (`REDIS_STREAM` not `mempool:pending:txs`)
- Prometheus multiprocess metrics directory misconfigured across containers

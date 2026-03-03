# Opportunity Engine

This module adds a paper-safe Opportunity Detection + Strategy Orchestration framework on top of the existing DEX pack router.

## Modules

- `bot/core/opportunity_engine/types.py`
  - `MarketEvent`
  - `Opportunity`
  - `TradePlan` (+ `TradeLeg`)
- `bot/detectors/base.py`
  - `BaseDetector.on_event(event) -> list[Opportunity]`
- `bot/detectors/cross_dex_arb.py`
  - Cross-DEX arb detector using router quote fanout
- `bot/detectors/routing_improvement.py`
  - Routing improvement detector (best route vs baseline dex)
- `bot/orchestrator/opportunity_orchestrator.py`
  - Priority queue orchestration, risk/sizing/sim gates, mode-aware planning

## Detection

### 1) Cross-DEX arb detector
- Uses `TradeRouter.arb_scan(intent)` over enabled DEX packs.
- Compares best and second-best outputs at probe size.
- Emits opportunity when edge in bps exceeds `OPP_MIN_EDGE_BPS`.
- Emits `size_candidates` for later sizing search.

### 2) Routing improvement detector
- Uses baseline dex (`event.dex_hint` or `ROUTE_BASELINE_DEX`).
- Compares best routed quote vs baseline quote.
- Emits opportunity when improvement exceeds `ROUTE_IMPROVEMENT_MIN_BPS`.

## Orchestrator

Priority score:
- `score = estimated_profit * confidence * freshness`
- freshness decays with age (half-life style)

Processing flow for each opportunity:
1. Read `operator_state.json`
2. Block on `kill_switch` or `state != TRADING`
3. Apply strategy enable/disable checks (`strategies_enabled` / `strategies_disabled`)
4. Risk checks (`MIN_EDGE_BPS`, basic constraints)
5. Sizing search across `size_candidates`
6. Quote/Build via router + selected DEX pack
7. Sim gate:
   - `live`: simulation required; reject on failure
   - `paper`: simulation attempted and recorded
   - `dryrun`: plan generation only
8. Produce `TradePlan` only if expected profit after costs is above `MIN_PROFIT_AFTER_COST`

Execution policy:
- Framework is additive and non-invasive.
- Live execution is callback-driven (`execute_cb`) and optional.

## Modes

- `dryrun`: detect + rank + build plans, no send
- `paper`: includes simulation and records fill-style metrics, no on-chain send
- `live`: requires simulation success before optional execution callback

Mode and guard source:
- `OPERATOR_STATE_PATH` JSON (`state`, `mode`, `kill_switch`)

## Metrics

Integrated into `ops/metrics.py`:
- `mevbot_opportunities_seen_total`
- `mevbot_opportunities_rejected_total{reason}`
- `mevbot_opportunities_simulated_total{dex,outcome}`
- `mevbot_opportunities_executed_total{dex,mode}`
- `mevbot_tx_sent_by_dex_type_total{dex,type}`
- `mevbot_opportunity_queue_depth`

Existing DEX metrics reused:
- quote latency/failure and sim failure

## Health Snapshot integration

`ops/health_snapshot.json` now includes:
- `top_opportunities_count`
- `funnel_10m` (seen/attempted/filled)
- `top_reject_reasons_10m`

## CLI Harness

Run dryrun harness:

```bash
python scripts/opportunity_orchestrator_cli.py --limit 5
```

Expected output:
- Detected opportunities per sample event
- Ranked decisions and generated plans (if any)

## Risk model knobs

- `MIN_EDGE_BPS`
- `MIN_PROFIT_AFTER_COST`
- `MAX_FEE`
- `OPP_GAS_COST_EST`
- `OPP_SLIPPAGE_BPS`
- `OPP_TTL_S`

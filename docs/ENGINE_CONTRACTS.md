# Engine Contracts

This document defines canonical contracts for the strategy engine.

## Modules

- `core/types_engine.py` (shim to `bot/core/types_engine.py`)
- `core/abc_engine.py` (shim to `bot/core/abc_engine.py`)

## Data Types

### MarketEvent
Represents normalized market updates from multiple sources.

Fields:
- `id`, `ts`, `family`, `chain`, `network`
- `kind`: `block|slot|log|pool_update|quote_update`
- optional context: `block_number`, `slot`, `tx_hash`, `pool`, `dex`, `token_in`, `token_out`, `amount_in`
- `payload`: free-form structured source payload
- `refs`: lightweight references (stream id, endpoint alias, etc.)

### Opportunity
Canonical opportunity produced by detectors.

Fields:
- `id`, `ts`, `family`, `chain`, `network`, `type`
- `signals`: numeric detector signals (spread, imbalance, edge proxy)
- `size_candidates`: candidate input sizes for sizing search
- `expected_edge_bps`, `confidence`
- `required_capabilities`: e.g. `quote`, `build`, `simulate`, `private_submit`
- `refs`: source references

### TradeIntent
Normalized quote/build intent.

Fields:
- `family`, `chain`, `network`
- `token_in`, `token_out`, `amount_in`
- `slippage_bps`, `ttl_s`
- `dex_preference`, `strategy`

### Quote
Canonical quote result.

Fields:
- `dex`, `expected_out`, `min_out`
- `price_impact_bps`, `fee_estimate`
- `route_summary`, `quote_latency_ms`

### TxPlan
Executable (or paper/dryrun) plan.

Fields:
- `family`, `chain`, `network`, `dex`, `mode`
- `legs[]`
- `raw_tx` (EVM path) or `instructions` (Solana path)
- `metadata`

### SimResult
Simulation outcome.

Fields:
- `ok`
- `error_code`, `error_message`
- `gas_estimate` / `compute_units`
- `logs`

### TxReceiptOrSignature
Execution return contract.

Fields:
- `tx_hash` or `signature`
- optional `block_number` / `slot`
- `status`, `metadata`

## Interfaces

### Detector
`on_event(event: MarketEvent) -> list[Opportunity]`

### Strategy
`build_plan(opportunity: Opportunity) -> TxPlan`

### Executor
- `simulate(plan: TxPlan) -> SimResult`
- `execute(plan: TxPlan) -> TxReceiptOrSignature`

## Notes

- Contracts are intentionally additive and framework-level.
- Existing router/DEX pack models can be adapted to these contracts via thin adapters.
- `family/chain/network` labels should remain canonicalized before metrics/log emission.

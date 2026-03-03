# Flashloans

This repo supports a pluggable flashloan provider abstraction for EVM execution plans.

## Provider Contract

`adapters/flashloans/base.py` defines:

- `supported_assets() -> Iterable[str]`
- `fee_bps() -> float`
- `build_flashloan_wrapper(plan: TxPlan) -> TxPlan`

`build_flashloan_wrapper` returns a `TxPlan` with flashloan metadata/instructions added, while preserving the underlying trade plan payload.

## Aave v3 Adapter

`adapters/flashloans/aave_v3.py` implements `AaveV3FlashloanProvider`.

Config is loaded from JSON (default `config/flashloans/aave_v3.json`), keyed by chain:

- `pool`
- `assets[]`
- `fee_bps`
- `executor_mode`: `predeployed` or `bytecode`
- `executor_address` (required for `predeployed`)
- `executor_bytecode` (required for `bytecode`)

## Packaging Approach

This implementation supports two execution packaging modes:

1. `predeployed`
   Use a configured executor contract address (`executor_address`) that receives flashloan + swap logic.
2. `bytecode`
   Carry executor bytecode in metadata (`executor_bytecode`) for deployment-aware executors.

No Solidity compilation is performed in this module; execution components consume metadata and decide final transaction construction.

## Orchestrator Decision Hook

`core/orchestrator.py` can optionally accept a `flashloan_provider`.

Decision inputs:

- `size` vs `INVENTORY_USD`
- loan fee (`provider.fee_bps()`) + `FLASHLOAN_GAS_OVERHEAD_USD`
- `FLASHLOAN_MIN_SIZE_USD`
- estimated trade profit after costs

If flashloan is selected, the orchestrator wraps the built plan and emits:

- `mevbot_flashloan_used_total{provider,...}`
- `mevbot_flashloan_fee_est_usd{provider,...}`


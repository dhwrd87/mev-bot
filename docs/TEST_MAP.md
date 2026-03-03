# Test Map

## How To Run
- Full suite: `make test`
- Pytest directly: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q -p pytest_asyncio.plugin`
- Golden path smoke (no sim required): `make smoke`
- Simulator smoke (no external deps): `make sim-smoke`

## Test Suites & Coverage

### Contract Tests (`tests/contract/`)
- `tests/contract/test_ports.py` : Interface compliance for adapters.
- `tests/contract/test_stealth_triggers_contract.py` : Stealth trigger shape/outputs.
- `tests/contract/test_v2_math_contract.py` : V2 math outputs.
- `tests/contract/test_sizer_contract.py` : Position sizing math.
- `tests/contract/test_risk_contract.py` : Risk manager outputs.

### Integration Tests (`tests/integration/`)
- `tests/integration/test_golden_path.py` : Golden path (Opportunity → Orchestrator → Risk → StealthExecutor(dry-run) → ResultIntake → Repo writes).
- `tests/integration/test_stealth_smoke.py` : Stealth smoke integration.

### Integration Tests (`bot/tests/integration/`)
- `bot/tests/integration/test_stealth_e2e.py` : Stealth flow; fork test skipped unless `POLYGON_RPC` + local anvil.
- `bot/tests/integration/test_hunter_e2e.py` : Hunter flow (builder submit); fork skeleton skipped unless `POLYGON_RPC`.

### Unit Tests (`bot/tests/unit/`)
Covers math, routing, detectors, orderflow, bundles, and simulators.
- Backrun calc: `bot/tests/unit/test_backrun_calc.py`
- Orderflow + router: `bot/tests/unit/test_orderflow*.py`
- Bundle builder: `bot/tests/unit/test_bundle_builder.py`
- Mempool monitor: `bot/tests/unit/test_mempool_monitor.py`
- V2/V3 math + quoter: `bot/tests/unit/test_v2_math.py`, `bot/tests/unit/test_quoter_guard.py`
- Stealth triggers: `bot/tests/unit/test_stealth_triggers.py`
- Permit2 + executor: `bot/tests/unit/test_permit2.py`, `bot/tests/unit/test_hunter_executor.py`
- Selector/sizer: `bot/tests/unit/test_selector_quoter.py`, `bot/tests/unit/test_sizer.py`
- Alerts/metrics: `bot/tests/unit/test_alerts.py`, `bot/tests/unit/test_metrics.py`

### Other Tests (`bot/tests/`)
- `bot/tests/test_simulator.py` : Pre-submit simulator behavior.
- `bot/tests/test_exact_output.py` : Exact-output encoding + simulation.
- `bot/tests/test_monitor_*` : Monitor reconnection/race-dedup behavior.

## Current Failures
- None observed in the latest run. (`71 passed, 3 skipped`)

## Skips / Conditional Tests
- Fork-dependent tests are guarded by `POLYGON_RPC` and require a local fork (anvil):
  - `bot/tests/integration/test_stealth_e2e.py`
  - `bot/tests/integration/test_hunter_e2e.py`


# Strategy Harness (Paper Mode)

`scripts/run_harness.py` runs a deterministic synthetic opportunity flow:

- emits `xarb` and `triarb` opportunities
- pushes them through router + orchestrator
- runs `quote -> build -> simulate`
- stays in operator `paper` mode (no real tx send)
- updates paper PnL/fees/drawdown metrics
- writes `ops/health_snapshot.json`

## Run

```bash
python scripts/run_harness.py --duration 15 --tick 0.25
```

Useful flags:

- `--sim-pattern ok,ok,fail` controls mocked simulation outcomes.
- `--operator-state-path runtime/harness_operator_state.json` sets harness state file.
- `--snapshot-path ops/health_snapshot.json` sets snapshot output.

## Output

The script prints a JSON summary with processed count, PnL, rejects, and snapshot path.

Snapshot includes:

- `opportunities_seen_10m`
- `opportunities_attempted_10m`
- `opportunities_executed_10m`
- `top_reject_reasons_10m`

# Chain Switch Verifier

`python3 scripts/verify_chain_switch.py` validates that a chain switch completed and DEX packs are consistent.

## What it checks
1. `mevbot_heartbeat_ts{family,chain,network}` switches to target labels within 30s.
2. Each enabled DEX pack returns at least one quote in dryrun verifier mode.

## Usage
- Set and verify base mainnet:
  - `python3 scripts/verify_chain_switch.py EVM:base --set-target`
- Set and verify sepolia:
  - `python3 scripts/verify_chain_switch.py EVM:sepolia --set-target`
- Set and verify solana:
  - `python3 scripts/verify_chain_switch.py SOL:solana --set-target --timeout-s 45`

## Flags
- `--set-target`: writes `chain_target`, `state=PAUSED`, `mode=dryrun` in operator state.
- `--operator-state`: path to operator state file. Default: `ops/operator_state.json`.
- `--metrics-url`: metrics endpoint. Default: `http://127.0.0.1:8000/metrics`.
- `--timeout-s`: heartbeat timeout. Default: `30`.

## Troubleshooting
- Heartbeat timeout:
  - check `curl -s http://127.0.0.1:8000/metrics | grep mevbot_heartbeat_ts`
  - check switch logs: `docker compose logs --tail=200 mev-bot`
- DEX quote failures:
  - verify profile config under `config/chains/<family>/<chain>-<network>.yaml`
  - verify RPC reachability and required addresses/program IDs for enabled packs.

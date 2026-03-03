# Daily Operations Runbook

## Switch Chain In 10 Seconds
1. Run `scripts/use-env.sh sepolia|amoy|mainnet` (this only edits `CHAIN=...` in `.env.runtime`).
2. Rebuild/restart: `docker compose up -d --build`.
3. Verify selection: `curl -s http://127.0.0.1:8000/health | jq`.
4. Confirm keys:
   - `chain`
   - `chain_id`
   - `rpc_http_selected`
   - `ws_endpoints_selected`
   - `ws_connected_endpoint`

### Standard Chain Profiles
- EVM mainnet/testnet profiles: `config/chains/evm/*.yaml`
  - e.g. `config/chains/evm/base-mainnet.yaml`
  - e.g. `config/chains/evm/sepolia-testnet.yaml`
- Solana profiles: `config/chains/sol/*.yaml`
  - e.g. `config/chains/sol/solana-devnet.yaml`

These profiles define:
- chain identity and endpoints
- enabled DEX packs
- required DEX pack addresses/program IDs

## Morning Checklist (9:00 AM UTC)
- [ ] Check overnight P&L
- [ ] Review any critical alerts
- [ ] Verify all strategies are active
- [ ] Check gas prices and adjust limits if needed
- [ ] Review competitor activity (new bots, strategies)
- [ ] Check for any failed transactions
- [ ] Verify backup systems are operational

## Continuous Monitoring
- Monitor Discord alerts channel
- Check Grafana dashboard every 2 hours
- Review large trades (>$1000 profit/loss)
- Monitor gas spikes
- Track success rates

## End of Day (21:00 UTC)
- [ ] Calculate daily P&L
- [ ] Archive logs to NAS
- [ ] Review strategy performance metrics
- [ ] Identify optimization opportunities
- [ ] Update risk limits if needed
- [ ] Backup configuration changes

smoke-orch:
\tdocker exec -it mev-bot-cores bash -lc "python3 scripts/smoke_orchestrator.py"

smoke-stealth:
\tdocker exec -it mev-bot-cores bash -lc "python3 scripts/smoke_stealth.py --network sepolia --stub-relays"

## Paper Mode
- Candidate detector is paper-only: it reads `mempool_tx` and writes `candidates`.
- It does not sign transactions, send transactions, or submit bundles.
- Start detector:
  - `PYTHONPATH=. python3 -m bot.workers.candidate_detector`
- Start evaluator:
  - `PYTHONPATH=. python3 -m bot.workers.candidate_evaluator`
- Check output:
  - `curl -s http://127.0.0.1:8000/candidates`
  - `curl -s http://127.0.0.1:8000/paper_report`

## Discord Operator Bot (Separate From Alerting)
- Existing alerting remains unchanged:
  - `DISCORD_WEBHOOK` (app alerts)
  - `ALERT_WEBHOOK` (Alertmanager relay)
- Operator process is separate and optional (`discord-operator` service, `ops` profile).

### Required env
- `DISCORD_OPERATOR_TOKEN`
- `DISCORD_OPERATOR_COMMAND_CHANNEL_ID`
- `DISCORD_OPERATOR_AUDIT_CHANNEL_ID`
- Optional:
  - `DISCORD_OPERATOR_PREFIX` (default `!`)
  - `DISCORD_OPERATOR_CONFIRM_TTL_S` (default `120`)
  - `DISCORD_OPERATOR_API_BASE` (default `http://mev-bot:8000`)
  - `DISCORD_OPERATOR_STATUS_CHANNEL_ID` (status card channel)
  - `DISCORD_OPERATOR_STATUS_REFRESH_S` (30-60 sec, default `45`)

### Run alongside stack
1. Start core stack:
   - `docker compose up -d --build`
2. Start operator bot:
   - `docker compose --profile ops up -d discord-operator`
3. Tail logs:
   - `docker compose logs -f discord-operator`

### Commands
- `!status`
- `!pause <reason>`
- `!resume <reason>`
- `!mode dryrun|paper|live <reason>`
- `!kill-switch on|off <reason>`
- `!chain set EVM:sepolia|EVM:amoy|SOL:solana <reason>`
- `!confirm <CODE>`

### Two-step confirmation
- Dangerous actions require confirm:
  - `!mode live ...`
  - `!kill-switch off ...`
- Bot returns a short confirmation code.
- Confirm with `!confirm CODE` before timeout.

### Pinned Status Card
- The operator bot maintains one pinned status embed in `DISCORD_OPERATOR_STATUS_CHANNEL_ID`.
- Refresh interval is controlled by `DISCORD_OPERATOR_STATUS_REFRESH_S` (clamped to 30-60 seconds).
- If the status message is deleted, the bot recreates it and pins the new message.

## Safe Chain Switching (Operator Flow)
1. In operator channel, issue `!chain set EVM:<chain>|SOL:solana <reason>`.
2. The bot enforces `PAUSED -> SYNCING -> READY`, validates connectivity, and writes an audit line.
3. Validation checks:
   - RPC reachability and head/slot advances
   - wallet address derivation
   - balance fetch
4. Trading remains blocked until manual `!resume`.

### Smoke Procedure
1. Start stack: `docker compose up -d --build`.
2. Verify API: `curl -sS http://127.0.0.1:8000/health`.
3. Trigger switch from operator:
   - `!chain set EVM:sepolia smoke-switch`
4. Check state transitions and chain selection:
   - `curl -sS http://127.0.0.1:8000/health | jq '.state,.chain_family,.chain,.rpc_http_selected'`
5. Confirm producer/consumer logs stay healthy:
   - `docker compose logs --tail=120 mempool-producer mempool-consumer`
6. Resume only when ready:
   - `!resume post-switch`

## Chain Switch Verifier
- Verifies two things after switch:
  1. `mevbot_heartbeat_ts{family,chain,network}` reflects target labels within 30s.
  2. Every enabled DEX pack returns at least one quote in dryrun verifier mode.

Run:
- `python3 scripts/verify_chain_switch.py EVM:base --set-target`
- `python3 scripts/verify_chain_switch.py EVM:sepolia --set-target`
- `python3 scripts/verify_chain_switch.py SOL:solana --set-target --timeout-s 45`

Notes:
- `--set-target` writes `chain_target`, forces `PAUSED`, and sets `mode=dryrun` in `ops/operator_state.json`.
- Override paths/endpoints if needed:
  - `--operator-state /app/ops/operator_state.json`
  - `--metrics-url http://127.0.0.1:8000/metrics`

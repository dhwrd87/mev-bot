# DEX Packs

DEX Packs provide a stable contract for quote/build/simulate per venue, selectable by chain.

## Contract

Core types are in [bot/core/types_dex.py](/Users/user1/stacks/mev/mev-bot/bot/core/types_dex.py):

- `TradeIntent`: `family, chain, network, dex_preference?, token_in, token_out, amount_in, slippage_bps, ttl_s, strategy`
- `Quote`: `dex, expected_out, min_out, price_impact_bps, fee_estimate, route_summary, quote_latency_ms`
- `TxPlan`: `family, chain, dex, raw_tx or instruction_bundle, value, metadata`
- `SimResult`: `ok, error_code, error_message, gas_estimate/compute_units, logs(optional)`

ABC is in [adapters/dex_packs/base.py](/Users/user1/stacks/mev/mev-bot/adapters/dex_packs/base.py):

- `quote(intent) -> Quote`
- `build(intent, quote) -> TxPlan`
- `simulate(plan) -> SimResult`
- `name()`
- `family_supported()`
- `chains_supported()`

## Registry

Registry is in [adapters/dex_packs/registry.py](/Users/user1/stacks/mev/mev-bot/adapters/dex_packs/registry.py).

Load order for enable/disable:

1. chain pack config profile (priority order):
   - `config/chains/<chain>.yaml` (legacy fallback)
   - `config/chains/<family>/<chain>.yaml`
   - `config/chains/<family>/<chain>-<network>.yaml` (preferred)
2. env overrides:
   - `DEX_PACKS_ENABLE=univ2,univ3`
   - `DEX_PACKS_DISABLE=univ2`
3. operator-state runtime toggles from `OPERATOR_STATE_FILE`:
   - `dex_packs_enabled` (optional allowlist)
   - `dex_packs_disabled` (optional denylist)

If `dex_packs_enabled` is present, it becomes authoritative, then disabled list is subtracted.

## Chain Config Schema

Chain config files are JSON-compatible YAML and live under `config/chains/`.
Preferred profile layout:

- `config/chains/evm/base-mainnet.yaml`
- `config/chains/evm/sepolia-testnet.yaml`
- `config/chains/sol/solana-devnet.yaml`

Example keys:

- `enabled_dex_packs: ["univ2","univ3","jupiter"]`
- `dex_packs.<instance>.type` (`evm_univ2|univ3|jupiter`)
- `dex_packs.<instance>.router` (univ2)
- `dex_packs.<instance>.factory` (univ2/univ3)
- `dex_packs.<instance>.fee_bps` (univ2, default 30)
- `dex_packs.<instance>.init_code_hash` (univ2, optional)
- `dex_packs.<instance>.quoter` (univ3)
- `dex_packs.<instance>.swap_router` (univ3)
- `dex_packs.<instance>.base_url` (jupiter)
- `dex_packs.<instance>.api_key` (jupiter, optional)

Multiple deployments per chain are supported by instance names, e.g.:

- `univ2_sushi` (`type: evm_univ2`)
- `univ2_pancake` (`type: evm_univ2`)

During chain switch, runtime validates enabled pack config fields:

- `evm_univ2|univ2`: `factory`, `router`
- `evm_univ3|univ3`: `factory`, `quoter`, `swap_router`
- `jupiter|sol_jupiter`: `base_url`

## Metrics

Exported in [ops/metrics.py](/Users/user1/stacks/mev/mev-bot/ops/metrics.py):

- `mevbot_dex_quote_total{dex,family,chain,network}`
- `mevbot_dex_quote_fail_total{dex,reason,family,chain,network}`
- `mevbot_dex_quote_latency_seconds{dex,family,chain,network}`
- `mevbot_dex_build_fail_total{dex,reason,family,chain,network}`
- `mevbot_dex_sim_fail_total{dex,reason,family,chain,network}`
- `mevbot_dex_route_hops{dex,family,chain,network}`

## Strategy Usage

Typical use:

1. Build `TradeIntent`.
2. `registry.reload(family=..., chain=..., network=...)`
3. `pack = registry.choose(intent.dex_preference)`
4. `quote = pack.quote(intent)`
5. `plan = pack.build(intent, quote)`
6. `sim = pack.simulate(plan)`
7. Record `mevbot_dex_*` metrics.

Current pack implementations are deterministic paper-safe stubs intended for incremental integration.

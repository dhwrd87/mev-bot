# Grafana Dashboard Lint Report

- Contract: `artifacts/prom_contract.json`
- Dashboards scanned: **19**
- Panel queries scanned: **128**
- Issues found: **13**

- Curated dashboard issues: **0**
- Archive dashboard issues: **13**

## Archive Dashboard Issues

_These do not affect provisioned dashboards but are listed for cleanup context._

| Dashboard | Panel | Ref | Metric | Problem | Suggested PromQL |
|---|---|---:|---|---|---|
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | A | `mevbot_pnl_realized_usd` | labels_not_on_metric: dex | `sum by (dex) (mevbot_pnl_realized_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | B | `mevbot_fees_total_usd` | labels_not_on_metric: dex | `sum by (dex) (mevbot_fees_total_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | C | `mevbot_drawdown_usd` | labels_not_on_metric: dex | `max by (dex) (mevbot_drawdown_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | Slippage Histogram by DEX (if available) | A | `mevbot_slippage_bps_bucket` | metric_not_in_contract | `Metric `mevbot_slippage_bps_bucket` not found in contract. Use an existing metric (examples: mevbot_dex_route_hops_bucket, mevbot_triarb_compute_seconds_bucket, mevbot_router_fanout_bucket).` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | Price Impact Histogram by DEX (if available) | A | `mevbot_price_impact_bps_bucket` | metric_not_in_contract | `Metric `mevbot_price_impact_bps_bucket` not found in contract. Use an existing metric (examples: mevbot_triarb_compute_seconds_bucket, mevbot_dex_route_hops_bucket, mevbot_rpc_latency_seconds_bucket).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | TPM (unique tx / 1m) | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | TPS (unique tx / 1s) | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | WS Connected (per endpoint) | A | `mevbot_mempool_ws_connected` | metric_not_in_contract | `Metric `mevbot_mempool_ws_connected` not found in contract. Use an existing metric (examples: mevbot_mempool_tps_legacy, mevbot_mempool_tps, mevbot_mempool_tpm).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Unique tx rate | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | A | `mevbot_mempool_rx_total` | metric_not_in_contract | `Metric `mevbot_mempool_rx_total` not found in contract. Use an existing metric (examples: mevbot_mempool_tps, mevbot_mempool_tpm, mevbot_attempts_total).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | B | `mevbot_mempool_rx_errors_total` | metric_not_in_contract | `Metric `mevbot_mempool_rx_errors_total` not found in contract. Use an existing metric (examples: mevbot_rpc_errors_total, mevbot_rpc_gettx_errors_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | C | `mevbot_mempool_reconnects_total` | metric_not_in_contract | `Metric `mevbot_mempool_reconnects_total` not found in contract. Use an existing metric (examples: mevbot_mode_outcomes_total, mevbot_mempool_tps, mevbot_attempts_total).` |
| `grafana/dashboards/ARCHIVE/mev-bot-dashboard.json` | Win Rate (Stealth, 24h) | A | `mevbot_win_rate` | metric_not_in_contract | `Metric `mevbot_win_rate` not found in contract. Use an existing metric (examples: mevbot_state, mevbot_bot_state, mevbot_chain_slot).` |

## Issues

| Dashboard | Panel | Ref | Metric | Problem | Suggested PromQL |
|---|---|---:|---|---|---|
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | A | `mevbot_pnl_realized_usd` | labels_not_on_metric: dex | `sum by (dex) (mevbot_pnl_realized_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | B | `mevbot_fees_total_usd` | labels_not_on_metric: dex | `sum by (dex) (mevbot_fees_total_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | PnL / Fees / Drawdown by DEX (live when labeled) | C | `mevbot_drawdown_usd` | labels_not_on_metric: dex | `max by (dex) (mevbot_drawdown_usd{family=~"$family", chain=~"$chain", network=~"$network", strategy=~"$strategy"}) or sum by (dex) (0 * mevbot_dex_quote_total{family=~"$family",chain=~"$chain",network=~"$network",dex=~"$dex"})` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | Slippage Histogram by DEX (if available) | A | `mevbot_slippage_bps_bucket` | metric_not_in_contract | `Metric `mevbot_slippage_bps_bucket` not found in contract. Use an existing metric (examples: mevbot_dex_route_hops_bucket, mevbot_triarb_compute_seconds_bucket, mevbot_router_fanout_bucket).` |
| `grafana/dashboards/ARCHIVE/dex_performance.json` | Price Impact Histogram by DEX (if available) | A | `mevbot_price_impact_bps_bucket` | metric_not_in_contract | `Metric `mevbot_price_impact_bps_bucket` not found in contract. Use an existing metric (examples: mevbot_triarb_compute_seconds_bucket, mevbot_dex_route_hops_bucket, mevbot_rpc_latency_seconds_bucket).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | TPM (unique tx / 1m) | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | TPS (unique tx / 1s) | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | WS Connected (per endpoint) | A | `mevbot_mempool_ws_connected` | metric_not_in_contract | `Metric `mevbot_mempool_ws_connected` not found in contract. Use an existing metric (examples: mevbot_mempool_tps_legacy, mevbot_mempool_tps, mevbot_mempool_tpm).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Unique tx rate | A | `mevbot_mempool_unique_tx_total` | metric_not_in_contract | `Metric `mevbot_mempool_unique_tx_total` not found in contract. Use an existing metric (examples: mevbot_dex_quote_total, mevbot_sim_bundle_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | A | `mevbot_mempool_rx_total` | metric_not_in_contract | `Metric `mevbot_mempool_rx_total` not found in contract. Use an existing metric (examples: mevbot_mempool_tps, mevbot_mempool_tpm, mevbot_attempts_total).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | B | `mevbot_mempool_rx_errors_total` | metric_not_in_contract | `Metric `mevbot_mempool_rx_errors_total` not found in contract. Use an existing metric (examples: mevbot_rpc_errors_total, mevbot_rpc_gettx_errors_total, mevbot_mempool_tps).` |
| `grafana/dashboards/ARCHIVE/mempool.json` | Per-endpoint intake & errors | C | `mevbot_mempool_reconnects_total` | metric_not_in_contract | `Metric `mevbot_mempool_reconnects_total` not found in contract. Use an existing metric (examples: mevbot_mode_outcomes_total, mevbot_mempool_tps, mevbot_attempts_total).` |
| `grafana/dashboards/ARCHIVE/mev-bot-dashboard.json` | Win Rate (Stealth, 24h) | A | `mevbot_win_rate` | metric_not_in_contract | `Metric `mevbot_win_rate` not found in contract. Use an existing metric (examples: mevbot_state, mevbot_bot_state, mevbot_chain_slot).` |

## Notes

- `metric_not_in_contract` typically means the metric is absent or stale.
- `labels_not_on_metric` means label matchers in PromQL cannot match that metric, often causing blank panels.

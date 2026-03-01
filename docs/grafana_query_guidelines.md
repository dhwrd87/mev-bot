# Grafana + PromQL Query Guidelines

Use these rules for all provisioned dashboards to avoid `bad_data` parse errors and empty panels caused by variable expansion.

## Variable Rules

- Always define these six selectors from heartbeat:
  - `family`: `label_values(mevbot_heartbeat_ts, family)`
  - `chain`: `label_values(mevbot_heartbeat_ts, chain)`
  - `network`: `label_values(mevbot_heartbeat_ts, network)`
  - `dex`: `label_values(mevbot_heartbeat_ts, dex)`
  - `strategy`: `label_values(mevbot_heartbeat_ts, strategy)`
  - `provider`: `label_values(mevbot_heartbeat_ts, provider)`
- For filter variables used in label matchers, enable:
  - `"includeAll": true`
  - `"customAllValue": ".*"`
- Use regex matchers with variables:
  - Good: `{family=~"$family",chain=~"$chain",network=~"$network"}`
  - Bad: `{family="$family",chain="$chain"}` when `All` is enabled

## PromQL Syntax Rules

- Label values must be quoted:
  - Good: `{chain="$chain"}`
  - Bad: `{chain=$chain}`
- For comparisons, use `==`, `!=`, `>`, `<`:
  - Good: `up{job="mev-bot"} == 1`
  - Bad: `up{job="mev-bot"} = 1`
- Do not use SQL-style expressions in PromQL.
- Avoid invalid assignment-like expressions:
  - Bad: `metric_a = on(label) (metric_b)`

## No-Data Prevention Rules

- If a panel depends on optional metrics, use fallback:
  - `some_optional_metric or vector(0)`
- Do not filter by labels that do not exist on that metric.
  - Example: `mevbot_sim_fail_total` has no `dex` label in this repo.
- Keep label cardinality low and selectors explicit.
- Mempool panels should always include canonical filters:
  - `{family=~"$family",chain=~"$chain",network=~"$network"}`
  - For endpoint series: aggregate by endpoint, e.g. `sum by (endpoint) (...)`

## Validation

Run:

```bash
./scripts/verify_dashboards.sh
```

This reads all provisioned dashboard JSON files under:
- `grafana/dashboards/00-operator`
- `grafana/dashboards/10-execution`
- `grafana/dashboards/20-strategy`
- `grafana/dashboards/90-infra`

Then it validates variable conventions and runs a subset of panel PromQL through Prometheus instant query API, failing on parse/API errors.

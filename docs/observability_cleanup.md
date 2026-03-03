# Observability Cleanup

Date: 2026-02-28

This cleanup follows findings in `docs/observability_audit.md` and keeps only curated Grafana dashboards provisioned.

## What was archived

Archived into `grafana/dashboards/ARCHIVE/`:

1. `mev-bot-dashboard.json`
- Original path: `grafana/dashboards/20-strategy/mev-bot-dashboard.json`
- Archived path: `grafana/dashboards/ARCHIVE/mev-bot-dashboard.json`
- Why: references mostly stale/nonexistent metrics and overlaps with curated dashboards.

2. `mev-bot-overview.json`
- Original path: `grafana/dashboards/90-infra/mev-bot-overview.json`
- Archived path: `grafana/dashboards/ARCHIVE/mev-bot-overview.json`
- Why: legacy overview with query issues (including typo) and overlap with mempool/operator/execution dashboards.

3. Grafana DB exports (safety snapshot before cleanup)
- Files: `grafana/dashboards/ARCHIVE/db_export__*.json`
- Why: preserve current live dashboard definitions before removing/disable-loading old dashboards.

## Curated dashboards kept

- `grafana/dashboards/00-operator/operator_overview.json`
- `grafana/dashboards/10-execution/execution_deep_dive.json`
- `grafana/dashboards/10-execution/private_orderflow.json`
- `grafana/dashboards/20-strategy/strategy_analytics.json`
- `grafana/dashboards/90-infra/mempool.json`

## Provisioning changes

`grafana/provisioning/dashboards/dashboards.yml` now uses explicit providers per curated folder:

- `/var/lib/grafana/dashboards/00-operator`
- `/var/lib/grafana/dashboards/10-execution`
- `/var/lib/grafana/dashboards/20-strategy`
- `/var/lib/grafana/dashboards/90-infra`

`ARCHIVE` is intentionally not provisioned.

## Result

Grafana should show only curated dashboards under:
- `00 Operator`
- `10 Execution`
- `20 Strategy`
- `90 Infra`

Archived dashboards remain version-controlled in git under `grafana/dashboards/ARCHIVE/`.

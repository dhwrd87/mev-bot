# Architecture Summary

## Service Boundaries

- `mev-bot` core service: strategy engine, stealth and hunter execution, risk management, and orchestration.
- External execution channels: private RPCs (Flashbots Protect, MEV Blocker), solver networks (CoW Protocol), and builder relays for bundles.
- Data stores: Postgres for core state and trade history; Redis for queues/streams; DuckDB for analytics and reporting.
- Observability stack: Prometheus metrics and Grafana dashboards; Discord webhook for alerting.
- Deployment/runtime: Docker Compose orchestrating `mev-bot`, `redis`, `postgres`, `grafana`, `prometheus`.

## Core Modules (by responsibility)

- Strategy engine: `BaseStrategy`, `StealthStrategy`, `HunterStrategy`, and `ExecutionMode` for mode selection and scoring.
- Risk management: `AdaptiveRiskManager` for position sizing (Kelly), exposure tracking, and go/no-go checks.
- Stealth execution: Permit2 handling, exact-output swap builder, and private orderflow routing.
- Hunter execution: mempool monitoring, sniper detection, backrun calculation, bundle construction, and builder submission.
- Alerting/ops: alert manager thresholds and Discord integration; operational runbooks and emergency stop flow.
- Security: encrypted key management with env-based unlock; access control for API usage.
- Testing: unit tests for strategy/risk logic, integration tests for full stealth flows.

## Data & Control Flow

- Detect opportunities (mempool or trade intent) → evaluate/score → risk gate → execute in stealth or hunter mode.
- Execution results feed trade records, metrics, alerts, and analytics rollups.
- Deployment safeguards include preflight checks, backup, migrations, smoke tests, and health verification.

# api/bootstrap_metrics.py
from bot.core.telemetry import orchestrator_decisions_total, risk_blocks_total, risk_state_gauge
orchestrator_decisions_total.labels(mode="init", reason="boot").inc()
risk_state_gauge.labels("daily_pnl_usd").set(0)
risk_state_gauge.labels("drawdown_pct").set(0)
risk_state_gauge.labels("consecutive_losses").set(0)

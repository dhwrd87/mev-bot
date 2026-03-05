# Trading Engine Deployment Guide

## Overview
The trading engine connects opportunity detection -> strategy decisions -> risk management -> execution -> metrics tracking.

## Prerequisites
- PostgreSQL database running
- Redis running
- Mempool producer/consumer working
- Discord bot configured
- Prometheus + Grafana running

## Deployment Steps

### 1. Database Migration
```bash
# Run trading engine migration
docker compose -f docker/docker-compose.yml exec mev-bot python3 scripts/migrate.py --one 0200_trading_engine.sql

# Verify tables created
docker compose -f docker/docker-compose.yml exec postgres psql -U mev_user -d mev_bot -c "\dt"
# Should show: trades, strategy_performance
```

### 2. Deploy Opportunity Processor
```bash
# Add service to docker-compose.yml (see Prompt 8)
# Rebuild and restart
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d

# Verify running
docker compose -f docker/docker-compose.yml ps opportunity-processor
# Should show: Up

# Check logs
docker compose -f docker/docker-compose.yml logs -f opportunity-processor
# Should show: "Opportunity processor started"
```

### 3. Configure Discord Commands
```bash
# Discord bot automatically loads trading commands on startup
# Verify in Discord:
# Type: /trades
# Type: /strategy
# Type: /pnl
# Type: /decisions
```

### 4. Import Grafana Dashboard
```bash
# Dashboard file already mounted from repo in docker-compose:
# ../grafana/dashboards -> /var/lib/grafana/dashboards

# Or import manually:
# 1. Open Grafana (http://localhost:3000)
# 2. Go to Dashboards -> Import
# 3. Upload grafana/dashboards/trading_overview.json
```

### 5. Validate Deployment
```bash
# Run validation script
python3 scripts/validate_trading_engine.py

# Expected output:
# ✓ Database tables exist
# ✓ Opportunity processor is running
# ✓ Mempool stream has activity
# ✓ Opportunities being detected
# ✓ Strategy decisions being made
# ✓ All trading metrics exposed
# ✓ Discord trading commands registered
# ✓ Trading dashboard available
# ✓ Trade recording functional
# ✓ Performance aggregation functional
# 10/10 checks passed
```

## Verification Checklist

### Metrics
- [ ] Check Prometheus: http://localhost:9090
  - Query: `rate(mevbot_opportunities_detected_total[5m])`
  - Should show non-zero rate
- [ ] Check Grafana: http://localhost:3000
  - Open "MEV Bot - Trading Overview" dashboard
  - Should show live data

### Discord
- [ ] Run `/trades limit:5` - Should show recent trades (or "No trades found" if none yet)
- [ ] Run `/pnl today` - Should show P&L summary
- [ ] Run `/strategy` - Should show strategy performance
- [ ] Check status card - Should show "Trading (Today)" section

### Database
```sql
-- Check for trades
SELECT COUNT(*) FROM trades;

-- Check recent trades
SELECT id, created_at, mode, strategy, executed, net_profit_usd
FROM trades
ORDER BY created_at DESC
LIMIT 10;

-- Check strategy performance
SELECT * FROM strategy_performance
WHERE date >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY net_profit_usd DESC;
```

### Logs
```bash
# Opportunity processor logs
docker compose -f docker/docker-compose.yml logs -f opportunity-processor
# Should show: opportunities detected, decisions made

# MEV bot API logs
docker compose -f docker/docker-compose.yml logs -f mev-bot
# Should show: orchestrator decisions, executions
```

## Troubleshooting

### No opportunities detected
**Symptoms**: `mevbot_opportunities_detected_total` stays at 0

**Solutions**:
1. Check mempool stream: `docker compose -f docker/docker-compose.yml exec redis redis-cli XLEN mempool:pending:txs`
   - If 0, check mempool producer is running
2. Check detectors are enabled in opportunity processor
3. Check logs for detector errors: `docker compose -f docker/docker-compose.yml logs opportunity-processor | grep ERROR`

### Decisions not being made
**Symptoms**: `mevbot_strategy_decisions_total` stays at 0

**Solutions**:
1. Check opportunity processor logs for errors
2. Verify orchestrator is initialized: `docker compose -f docker/docker-compose.yml logs opportunity-processor | grep "orchestrator"`
3. Check operator state is not paused: `docker compose -f docker/docker-compose.yml exec postgres psql -U mev_user -d mev_bot -c "SELECT * FROM ops_state WHERE k='paused'"`

### Trades not recorded
**Symptoms**: `/trades` command shows no trades

**Solutions**:
1. Check database connection:
   - `docker compose -f docker/docker-compose.yml exec mev-bot python3 -c "import os, psycopg; psycopg.connect(os.environ['DATABASE_URL']); print('ok')"`
2. Check trade recorder logs: `docker compose -f docker/docker-compose.yml logs mev-bot | grep "Trade recorded"`
3. Verify migration ran: `SELECT * FROM trades LIMIT 1;`

### Discord commands not working
**Symptoms**: Commands not showing in Discord

**Solutions**:
1. Check bot is connected: `docker compose -f docker/docker-compose.yml logs discord-operator | grep "ready"`
2. Verify commands registered: `docker compose -f docker/docker-compose.yml logs discord-operator | grep "trading commands"`
3. Re-sync commands: Restart discord-operator service

### Metrics not showing in Grafana
**Symptoms**: Dashboard panels show "No data"

**Solutions**:
1. Check Prometheus scraping mev-bot: http://localhost:9090/targets
2. Verify metrics exposed: `curl http://localhost:9100/metrics | grep mevbot_opportunities`
3. Check Prometheus datasource in Grafana: Configuration -> Data Sources

## Performance Tuning

### High opportunity queue depth
If `mevbot_pending_opportunities` stays high:
- Increase opportunity processor instances (scale horizontally)
- Optimize detector logic
- Increase Redis consumer group workers

### Slow decision latency
If `mevbot_strategy_decision_latency_ms` p95 > 100ms:
- Profile orchestrator._select_strategy()
- Cache operator state reads
- Optimize risk manager queries

### Database performance
If trade recording is slow:
- Add more indexes on common query patterns
- Partition trades table by date
- Use connection pooling

## Monitoring Alerts

Recommended Prometheus alerts:
```yaml
groups:
  - name: trading_engine
    rules:
      - alert: NoOpportunitiesDetected
        expr: rate(mevbot_opportunities_detected_total[10m]) == 0
        for: 10m
        annotations:
          summary: "No opportunities detected for 10 minutes"
          
      - alert: HighRiskRejectionRate
        expr: rate(mevbot_risk_rejections_total[5m]) > rate(mevbot_risk_approvals_total[5m])
        for: 5m
        annotations:
          summary: "Risk rejection rate exceeds approval rate"
          
      - alert: LowWinRate
        expr: mevbot_trade_win_rate_pct{window="24h"} < 30
        for: 1h
        annotations:
          summary: "Win rate below 30% for 1 hour"
          
      - alert: HighExecutionLatency
        expr: histogram_quantile(0.95, mevbot_execution_latency_ms) > 5000
        for: 5m
        annotations:
          summary: "P95 execution latency above 5 seconds"
```

## Rollback Procedure

If deployment fails:
```bash
# 1. Stop opportunity processor
docker compose -f docker/docker-compose.yml stop opportunity-processor

# 2. Rollback database migration (if needed)
docker compose -f docker/docker-compose.yml exec postgres psql -U mev_user -d mev_bot
# DROP TABLE trades;
# DROP TABLE strategy_performance;

# 3. Remove service from docker-compose.yml

# 4. Restart stack
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d

# 5. Verify original functionality works
docker compose -f docker/docker-compose.yml ps
curl http://localhost:8000/health
```

## Next Steps

After successful deployment:
1. Run in **paper mode** for 24-48 hours
2. Monitor metrics and logs closely
3. Review trade decisions in database
4. Analyze strategy performance
5. Tune strategy selection heuristics based on data
6. Gradually enable live mode with small position limits

---

**Support**: Check logs, metrics, and database for debugging. All decisions and trades are fully logged with context for analysis.

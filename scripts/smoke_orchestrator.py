#!/usr/bin/env python3
import os, sys, asyncio
import aiohttp

from types import SimpleNamespace as SNS
from bot.risk.adaptive import AdaptiveRiskManager, RiskConfig
from bot.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from bot.core.telemetry import orchestrator_decisions_total, risk_blocks_total, risk_state_gauge

API_URL = os.getenv("SMOKE_API_URL","http://localhost:8000")

# --- tiny concrete strategies for smoke ---
class StealthStub:
    async def execute_like(self, opp):
        # emulate small fee + low gas ratio
        return {"ok": True, "pnl_usd": opp.get("expected_profit_usd", 8.0) - 0.5}

class HunterStub:
    async def execute_like(self, opp):
        # emulate bigger swings
        base = opp.get("expected_profit_usd", 12.0)
        return {"ok": True, "pnl_usd": base - opp.get("gas_cost_usd", 2.0)}

async def get_json(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return r.status, (await r.text())

def expect(cond, msg):
    if not cond:
        print(f"❌ {msg}"); sys.exit(1)
    print(f"✅ {msg}")

async def main():
    # 1) service
    st, body = await get_json(f"{API_URL}/health")
    expect(st==200 and "ok" in body.lower(), "API /health up")
    st, text = await get_json(f"{API_URL}/metrics")
    expect(st==200 and "mevbot_" in text, "Metrics available")

    # 2) orchestrator + risk
    risk = AdaptiveRiskManager(RiskConfig(capital_usd=10000, max_position_size_pct=5.0, max_daily_loss_usd=50.0, max_consecutive_losses=3))
    orch = Orchestrator(OrchestratorConfig(gas_spike_gwei=120, min_snipers_active=1), risk, StealthStub(), HunterStub())

    # 3) opportunities (two losers to hit drawdown/cons losses)
    ops = [
        {"exact_output": True,  "type":"", "gas_gwei": 40,  "size_usd": 300, "expected_profit_usd": 8.0},   # stealth
        {"vulnerable_flow": True,"detected_snipers":1,"gas_gwei": 40,"size_usd": 300, "expected_profit_usd": 15.0}, # hunter
        {"vulnerable_flow": True,"detected_snipers":1,"gas_gwei": 200,"size_usd": 300, "expected_profit_usd": 9.0}, # stealth (gas spike)
        {"exact_output": False, "gas_gwei": 50, "size_usd": 600, "expected_profit_usd": -20.0},  # loss
        {"exact_output": False, "gas_gwei": 50, "size_usd": 600, "expected_profit_usd": -20.0},  # loss
        {"vulnerable_flow": True,"detected_snipers":1,"gas_gwei": 60,"size_usd": 1200, "expected_profit_usd": 20.0}, # may be blocked by drawdown or pos cap
    ]

    results = []
    for i, opp in enumerate(ops):
        r = await orch.handle(opp)
        results.append(r)

    # 4) assertions
    dec_ok = any(r.get("ok") for r in results)
    expect(dec_ok, "At least one opportunity executed OK")
    # Expect at least one risk block (after two losses)
    expect(any(not r.get("ok") and r.get("blocked_by") for r in results), "At least one trade blocked by risk gate")

    # 5) metrics presence
    st, text2 = await get_json(f"{API_URL}/metrics")
    expect("mevbot_orchestrator_decisions_total" in text2, "Orchestrator decision counter present")
    expect("mevbot_risk_blocks_total" in text2, "Risk block counter present")
    expect("mevbot_risk_state" in text2, "Risk state gauges present")

    print("\\n🎉 Orchestration & Risk smoke PASS")

if __name__ == "__main__":
    asyncio.run(main())

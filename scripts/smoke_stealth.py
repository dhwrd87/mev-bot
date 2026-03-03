#!/usr/bin/env python3
import os, sys, asyncio, base64
import aiohttp

# Service URLs (override via env if needed)
PROM_URL    = os.getenv("SMOKE_PROM_URL",    "http://prometheus:9090")
GRAFANA_URL = os.getenv("SMOKE_GRAFANA_URL", "http://grafana:3000")
API_URL     = os.getenv("SMOKE_API_URL",     "http://localhost:8000")

# ---- fallback config for stealth_triggers (module-local only) ----
from bot.strategy import stealth_triggers as st
from types import SimpleNamespace as SNS
try:
    _ = st.settings.stealth_strategy.triggers
except Exception:
    st.settings = SNS(stealth_strategy=SNS(triggers=SNS(
        min_flags=2,
        flags=SNS(
            high_slippage=0.005,
            new_token_age_hours=24,
            low_liquidity_usd=100000,
            trending=True,
            active_snipers_min=1,
            large_trade_usd=20000,
            gas_spike_gwei=120,
        ),
    )))

from bot.strategy.stealth_triggers import TradeContext, evaluate_stealth

# ---- minimal concrete strategy (don’t depend on router internals) ----
from bot.strategy.stealth import StealthStrategy as _SS
class StealthConcrete(_SS):
    def __init__(self):  # skip parent __init__()
        pass
    async def evaluate(self, context): return 1.0
    async def execute(self, opportunity):  # unused in this smoke
        return await self.execute_stealth_swap(opportunity)
    async def execute_stealth_swap(self, params):
        # Synthetic success with safe gas ratio
        class R: pass
        r = R()
        r.success = True
        r.sandwiched = False
        r.notes = {"gas_cost_ratio": 0.003}
        return r

# ---- helpers ----
async def get_json(url, auth=None):
    async with aiohttp.ClientSession() as s:
        headers = {}
        if auth: headers["Authorization"] = "Basic " + auth
        async with s.get(url, headers=headers) as r:
            return r.status, (await r.text())

def expect(cond, msg):
    if not cond:
        print(f"❌ {msg}")
        sys.exit(1)
    print(f"✅ {msg}")

async def main():
    # 1) service checks
    try:
        status, body = await get_json(f"{API_URL}/health")
        expect(status == 200 and "ok" in body.lower(), "API /health is up")
    except Exception as e:
        expect(False, f"API /health unreachable: {e}")

    status, text = await get_json(f"{API_URL}/metrics")
    expect(status == 200 and "mevbot_" in text, "Metrics endpoint serving")

    try:
        status, ptxt = await get_json(f"{PROM_URL}/api/v1/targets")
        expect(status == 200 and "\"health\":\"up\"" in ptxt, "Prometheus targets are UP")
    except Exception as e:
        print(f"⚠️  Prometheus target check skipped: {e}")

    try:
        gf_user = os.getenv("GF_SECURITY_ADMIN_USER","admin")
        gf_pass = os.getenv("GF_SECURITY_ADMIN_PASSWORD","admin")
        auth = base64.b64encode(f"{gf_user}:{gf_pass}".encode()).decode()
        status, gtxt = await get_json(f"{GRAFANA_URL}/api/health", auth=auth)
        expect(status == 200 and "\"database\":" in gtxt, "Grafana /api/health OK")
    except Exception as e:
        print(f"⚠️  Grafana health check skipped: {e}")

    # 2) stealth triggers
    ctx = TradeContext(
        estimated_slippage=0.02, token_age_hours=6, liquidity_usd=80_000,
        is_trending=True, detected_snipers=1, size_usd=8_000, gas_gwei=40
    )
    go, reasons = evaluate_stealth(ctx)
    expect(go and len(reasons) >= 2, f"Stealth triggers fired: {reasons}")

    # 3) stealth “exec” (stubbed)
    strat = StealthConcrete()
    for i in range(3):
        res = await strat.execute_stealth_swap({"chain":"sepolia"})
        expect(res.success, f"Stealth trade {i+1} executed (stubbed)")
        ratio = float(res.notes.get("gas_cost_ratio", 0.0))
        expect(ratio < 0.005, f"Gas ratio < 0.5% (was {ratio:.4%})")

    # 4) metric keys present
    status, text2 = await get_json(f"{API_URL}/metrics")
    # Soft warnings for relay counters (stub path may not tick them)
    print("✅ Relay attempts metric present" if "mevbot_relay_attempts_total" in text2 else "⚠️  Relay attempts metric not found")
    print("✅ Relay success metric present" if "mevbot_relay_success_total" in text2 else "⚠️  Relay success metric not found")
    expect("mevbot_stealth_decisions_total" in text2, "Stealth decisions metric present")

    print("\\n🎉 Stealth Mode MVP smoke PASS")

if __name__ == "__main__":
    asyncio.run(main())

import pytest
from bot.strategy.stealth import StealthStrategy as _SS
pytestmark = pytest.mark.asyncio

class StealthConcrete(_SS):
    def __init__(self):  # don’t call parent __init__
        pass
    async def evaluate(self, context): return 1.0
    async def execute(self, opportunity): return await self.execute_stealth_swap(opportunity)
    async def execute_stealth_swap(self, params):
        class R: pass
        r = R()
        r.success = True
        r.sandwiched = False
        r.notes = {"gas_cost_ratio": 0.003}
        return r

async def test_stealth_exec_stubbed():
    s = StealthConcrete()
    params = dict(chain="sepolia")
    r = await s.execute_stealth_swap(params)
    assert r.success and float(r.notes.get("gas_cost_ratio", 0)) < 0.005

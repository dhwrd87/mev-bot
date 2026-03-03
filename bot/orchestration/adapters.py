# bot/orchestration/adapters.py
class StealthAdapter:
    def __init__(self, strat): self.s = strat
    async def execute_like(self, opp: dict):
        # map generic 'opp' to your stealth execute params
        txr = await self.s.execute_stealth_swap({
            "chain": opp.get("chain","sepolia"),
            "token_in": opp["token_in"],
            "token_out": opp["token_out"],
            "amount_in": opp["amount_in"],
            "desired_output": opp["desired_output"],
            "max_input": opp["max_input"],
            "router": opp["router"],
            "sender": opp["sender"],
            "recipient": opp["recipient"],
            "pool_fee": opp.get("pool_fee",3000),
            "eth_usd": opp.get("eth_usd",2500.0),
            "size_usd": opp.get("size_usd",0),
            "detected_snipers": opp.get("detected_snipers",0),
            "value_wei": opp.get("value_wei",0),
        })
        return {"ok": bool(txr.success), "pnl_usd": opp.get("expected_profit_usd", 0)}

class HunterAdapter:
    def __init__(self, strat): self.s = strat
    async def execute_like(self, opp: dict):
        # map your backrun/bundle call here, return expected pnl if you have it
        r = await self.s.execute_backrun(opp["target_signed_tx"], opp["our_signed_tx"], opp["current_block"])
        return {"ok": bool(r.get("ok")), "pnl_usd": opp.get("expected_profit_usd", 0)}

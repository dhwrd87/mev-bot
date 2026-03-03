from dataclasses import dataclass
from typing import Dict, Tuple, Optional

from bot.core.telemetry import orchestrator_decisions_total
from bot.ports.interfaces import TradeRepo, AlertRepo

@dataclass
class OrchestratorConfig:
    gas_spike_gwei: float = 120.0  # bias to stealth when gas high
    min_snipers_active: int = 1    # bias to stealth/hunter based on activity

class Orchestrator:
    def __init__(
        self,
        cfg: OrchestratorConfig,
        risk_mgr,
        stealth_strategy,
        hunter_strategy,
        trade_repo: Optional[TradeRepo] = None,
        alert_repo: Optional[AlertRepo] = None,
    ):
        self.cfg = cfg
        self.risk = risk_mgr
        self.stealth = stealth_strategy
        self.hunter = hunter_strategy
        self.trade_repo = trade_repo
        self.alert_repo = alert_repo

    async def handle(self, opp: Dict) -> Dict:
        mode, reason = self.pick_mode(opp)
        orchestrator_decisions_total.labels(mode=mode, reason=reason).inc()

        ok, gate_reason = self.risk.should_execute(opp)
        if not ok:
            return {"ok": False, "blocked_by": gate_reason, "mode": mode, "reason": reason}

        # --- Execute
        res = await (self.stealth if mode == "stealth" else self.hunter).execute_like(opp)
        pnl = float(res.get("pnl_usd", 0.0))
        self.risk.record_result(pnl)

        trade_id = None
        if self.trade_repo:
            row = {
                "mode": mode,
                "chain": opp.get("chain","sepolia"),
                "token_in": opp.get("token_in"),
                "token_out": opp.get("token_out"),
                "pair": f"{opp.get('token_in','')}-{opp.get('token_out','')}",
                "size_usd": opp.get("size_usd"),
                "expected_profit_usd": opp.get("expected_profit_usd"),
                "status": "submitted",
                "tx_hash": res.get("tx_hash"),            # stealth
                "bundle_tag": res.get("bundle_tag"),      # hunter
                "builder": res.get("builder") or res.get("relay"),
                "context": {"reason": reason, **opp}
            }
            trade_id = await self.trade_repo.insert_trade(row)

        return {"ok": res.get("ok", False), "mode": mode, "reason": reason, "pnl_usd": pnl, "trade_id": trade_id}

    def pick_mode(self, opp: Dict) -> Tuple[str, str]:
        # Very simple heuristic: high gas or trending/new → stealth; else hunter
        if opp.get("type") == "stealth_hint":
            return "stealth", "hint"
        if opp.get("gas_gwei", 0) >= self.cfg.gas_spike_gwei:
            return "stealth", "gas_spike"
        if opp.get("detected_snipers", 0) >= self.cfg.min_snipers_active and opp.get("vulnerable_flow"):
            return "hunter", "snipers_active"
        # default: stealth for exact-output flows, otherwise hunter
        return ("stealth", "exact_output") if opp.get("exact_output") else ("hunter", "default")

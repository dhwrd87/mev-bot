from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from bot.core.opportunity_engine.types import Opportunity


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str


@dataclass(frozen=True)
class RiskGateConfig:
    min_edge_bps: float = 2.0
    min_confidence: float = 0.05
    min_profit_est_usd: float = 0.0
    max_fee_usd: float = 1_000_000.0

    @classmethod
    def from_env(cls) -> "RiskGateConfig":
        return cls(
            min_edge_bps=float(os.getenv("MIN_EDGE_BPS", "2.0")),
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.05")),
            min_profit_est_usd=float(os.getenv("MIN_PROFIT_AFTER_COST", "0.0")),
            max_fee_usd=float(os.getenv("MAX_FEE", "1000000000000")),
        )


def operator_gate(operator_state: Mapping[str, Any]) -> GateResult:
    if bool(operator_state.get("kill_switch", False)):
        return GateResult(False, "operator_kill_switch")
    if str(operator_state.get("state", "UNKNOWN")).upper() != "TRADING":
        return GateResult(False, "operator_not_trading")
    return GateResult(True, "ok")


def cheap_opportunity_gate(opp: Opportunity, cfg: RiskGateConfig) -> GateResult:
    if float(opp.expected_edge_bps) < float(cfg.min_edge_bps):
        return GateResult(False, "edge_below_threshold")
    if float(opp.confidence) < float(cfg.min_confidence):
        return GateResult(False, "confidence_below_threshold")
    if not opp.size_candidates:
        return GateResult(False, "missing_size_candidates")
    if any(int(s) <= 0 for s in opp.size_candidates):
        return GateResult(False, "invalid_size_candidates")
    token_in = str(opp.constraints.get("token_in") or "")
    token_out = str(opp.constraints.get("token_out") or "")
    if not token_in or not token_out:
        return GateResult(False, "missing_tokens")
    return GateResult(True, "ok")

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from adapters.flashloans.base import FlashloanProvider
from bot.core.opportunity_engine.types import Opportunity
from bot.core.types_dex import TxPlan


@dataclass(frozen=True)
class FlashloanArbPlan:
    provider: str
    token_in: str
    borrow_amount: int
    repay_amount: int
    fee_bps: float
    path_tokens: List[str]
    path_dexes: List[str]
    expected_profit_after_costs: float
    kind: str


class FlashloanArbStrategy:
    def __init__(self, provider: FlashloanProvider) -> None:
        self.provider = provider

    def build_plan(
        self,
        *,
        opportunity: Opportunity,
        size: int,
        expected_profit_after_costs: float,
    ) -> FlashloanArbPlan:
        token_in = str(opportunity.constraints.get("token_in") or "")
        token_out = str(opportunity.constraints.get("token_out") or "")
        if not token_in:
            raise ValueError("missing_token_in")

        fee_bps = float(self.provider.fee_bps())
        repay = int(round(float(size) * (1.0 + max(0.0, fee_bps) / 10_000.0)))

        kind = str(opportunity.type or "").strip().lower()
        if kind == "triarb":
            path_tokens = [str(x) for x in (opportunity.constraints.get("path_tokens") or []) if str(x)]
            if not path_tokens:
                mid1 = str(opportunity.constraints.get("token_mid_1") or "")
                mid2 = str(opportunity.constraints.get("token_mid_2") or "")
                path_tokens = [token_in, mid1, mid2, token_in]
            path_dexes = [str(x) for x in (opportunity.constraints.get("path_dexes") or []) if str(x)]
        else:
            # xARB defaults to token_in -> token_out -> token_in
            path_tokens = [token_in, token_out, token_in]
            buy_dex = str(opportunity.constraints.get("best_dex") or "")
            sell_dex = str(opportunity.constraints.get("sell_dex") or opportunity.constraints.get("second_best_dex") or "")
            path_dexes = [buy_dex, sell_dex]

        return FlashloanArbPlan(
            provider=self.provider.name(),
            token_in=token_in,
            borrow_amount=int(size),
            repay_amount=int(repay),
            fee_bps=fee_bps,
            path_tokens=path_tokens,
            path_dexes=path_dexes,
            expected_profit_after_costs=float(expected_profit_after_costs),
            kind=kind or "xarb",
        )

    def attach_to_txplan(self, tx_plan: TxPlan, arb_plan: FlashloanArbPlan) -> TxPlan:
        md = dict(tx_plan.metadata or {})
        ib = dict(tx_plan.instruction_bundle or {})
        md["flashloan_arb"] = {
            "provider": arb_plan.provider,
            "kind": arb_plan.kind,
            "token_in": arb_plan.token_in,
            "borrow_amount": int(arb_plan.borrow_amount),
            "repay_amount": int(arb_plan.repay_amount),
            "fee_bps": float(arb_plan.fee_bps),
            "path_tokens": list(arb_plan.path_tokens),
            "path_dexes": list(arb_plan.path_dexes),
            "expected_profit_after_costs": float(arb_plan.expected_profit_after_costs),
        }
        ib["flashloan_arb_bundle"] = {
            "borrow": {"token": arb_plan.token_in, "amount": int(arb_plan.borrow_amount)},
            "swap_legs": [
                {"token_in": arb_plan.path_tokens[i], "token_out": arb_plan.path_tokens[i + 1], "dex": arb_plan.path_dexes[i] if i < len(arb_plan.path_dexes) else "unknown"}
                for i in range(max(0, len(arb_plan.path_tokens) - 1))
            ],
            "repay": {"token": arb_plan.token_in, "amount": int(arb_plan.repay_amount)},
        }
        return TxPlan(
            family=tx_plan.family,
            chain=tx_plan.chain,
            dex=tx_plan.dex,
            value=tx_plan.value,
            metadata=md,
            raw_tx=tx_plan.raw_tx,
            instruction_bundle=ib,
        )

    @staticmethod
    def bucket_sim_failure(error_code: str, error_message: str = "", logs: List[str] | None = None) -> str:
        code = str(error_code or "").strip().lower()
        msg = str(error_message or "").strip().lower()
        blob = " ".join([code, msg] + [str(x).lower() for x in (logs or [])])
        if "insufficient" in blob or "liquidity" in blob:
            return "sim_insufficient_liquidity"
        if "slippage" in blob or "min_out" in blob:
            return "sim_slippage"
        if "repay" in blob or "flashloan" in blob:
            return "sim_repay_failed"
        if "revert" in blob:
            return "sim_revert"
        if "timeout" in blob:
            return "sim_timeout"
        if "rpc" in blob:
            return "sim_rpc_error"
        return "sim_failed_other"

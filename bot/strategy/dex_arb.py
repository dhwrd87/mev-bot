from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.operator_control import get_operator_state
from bot.core.opportunity_engine.types import TradeLeg, TradePlan
from bot.core.router import TradeRouter
from bot.core.types_dex import TradeIntent, TxPlan as DexTxPlan
from bot.exec.engine import ExecutionEngine
from bot.exec.orderflow import PrivateOrderflowRouter, TxTraits
from bot.strategy.base import BaseStrategy, TransactionResult


def _as_int(v: Any, d: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return d


def _as_float(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _as_bool(v: Any, d: bool = False) -> bool:
    if v is None:
        return d
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


class DexArbStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        router: TradeRouter,
        registry: DEXPackRegistry,
        chain: str,
        orderflow_router: Optional[PrivateOrderflowRouter] = None,
        execution_engine: Optional[ExecutionEngine] = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.chain = str(chain)
        self.orderflow = orderflow_router or PrivateOrderflowRouter.from_env()
        self.engine = execution_engine or ExecutionEngine()

        self.min_edge_bps = _as_float(os.getenv("ARB_MIN_EDGE_BPS", "5"), 5.0)
        self.min_profit_usd = _as_float(os.getenv("ARB_MIN_PROFIT_USD", "2.0"), 2.0)
        self.use_flash_loan_default = _as_bool(os.getenv("ARB_USE_FLASH_LOAN", "false"), False)

    def _intent_from_context(self, context: Dict[str, Any]) -> TradeIntent:
        payload = context.get("payload") if isinstance(context.get("payload"), dict) else {}
        return TradeIntent(
            family=str(context.get("family", "evm")),
            chain=str(context.get("chain", self.chain)),
            network=str(context.get("network", "testnet")),
            token_in=str(context.get("token_in") or payload.get("token_in") or ""),
            token_out=str(context.get("token_out") or payload.get("token_out") or ""),
            amount_in=max(1, _as_int(context.get("amount_in", payload.get("amount_in", 0)), 0)),
            slippage_bps=max(1, _as_int(context.get("slippage_bps", payload.get("slippage_bps", 50)), 50)),
            ttl_s=max(1, _as_int(context.get("ttl_s", payload.get("ttl_s", 30)), 30)),
            strategy="dex_arb",
            dex_preference=(str(context.get("dex_preference")).strip() or None)
            if context.get("dex_preference") is not None
            else None,
        )

    async def evaluate(self, context: Dict[str, Any]) -> float:
        intent = self._intent_from_context(context)
        quotes = self.router.arb_scan(intent)
        good = [q for q in quotes if q.ok and q.quote is not None]
        if len(good) < 2:
            return 0.0
        best = good[0].quote
        worst = good[-1].quote
        if best is None or worst is None:
            return 0.0
        if int(worst.expected_out) <= 0:
            return 0.0
        edge_bps = ((float(best.expected_out) - float(worst.expected_out)) / float(worst.expected_out)) * 10_000.0
        return max(0.0, min(1.0, edge_bps / 1000.0))

    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        intent = self._intent_from_context(opportunity)
        table = self.router.arb_scan(intent)
        good = [q for q in table if q.ok and q.quote is not None]
        if len(good) < 2:
            return TransactionResult(success=False, mode="dex_arb", notes={"reason": "insufficient_quotes"})

        best_sel = self.router.route(intent)
        if best_sel is None or best_sel.quote is None:
            return TransactionResult(success=False, mode="dex_arb", notes={"reason": "no_best_route"})
        worst = good[-1]
        worst_quote = worst.quote
        assert worst_quote is not None

        edge_bps = ((float(best_sel.quote.expected_out) - float(worst_quote.expected_out)) / max(1.0, float(worst_quote.expected_out))) * 10_000.0
        if edge_bps < self.min_edge_bps:
            return TransactionResult(
                success=False,
                mode="dex_arb",
                notes={"reason": "edge_below_threshold", "edge_bps": edge_bps},
            )

        amount_in_usd = _as_float(opportunity.get("amount_in_usd", opportunity.get("notional_usd", intent.amount_in)), float(intent.amount_in))
        est_profit_usd = amount_in_usd * max(0.0, edge_bps) / 10_000.0
        if est_profit_usd < self.min_profit_usd:
            return TransactionResult(
                success=False,
                mode="dex_arb",
                notes={"reason": "profit_below_threshold", "profit_est_usd": est_profit_usd},
            )

        best_pack = self.registry.get(best_sel.dex)
        worst_pack = self.registry.get(worst.dex)
        if best_pack is None or worst_pack is None:
            return TransactionResult(success=False, mode="dex_arb", notes={"reason": "missing_dex_pack"})

        buy_built = best_pack.build(intent, best_sel.quote)
        sell_intent = TradeIntent(
            family=intent.family,
            chain=intent.chain,
            network=intent.network,
            token_in=intent.token_out,
            token_out=intent.token_in,
            amount_in=max(1, int(best_sel.quote.expected_out)),
            slippage_bps=intent.slippage_bps,
            ttl_s=intent.ttl_s,
            strategy="dex_arb",
            dex_preference=worst.dex,
        )
        sell_built = worst_pack.build(sell_intent, worst_quote)

        two_leg_plan = TradePlan(
            id=f"dexarb:{int(time.time() * 1000)}",
            ts=time.time(),
            family=intent.family,
            chain=intent.chain,
            network=intent.network,
            opportunity_id=str(opportunity.get("id", "")),
            mode="live",
            dex_pack=f"{best_sel.dex}->{worst.dex}",
            ttl_s=intent.ttl_s,
            max_fee=_as_float(opportunity.get("max_fee", 0.0), 0.0),
            slippage_bps=intent.slippage_bps,
            expected_profit_after_costs=est_profit_usd,
            legs=[
                TradeLeg(
                    dex=best_sel.dex,
                    token_in=intent.token_in,
                    token_out=intent.token_out,
                    amount_in=intent.amount_in,
                    expected_out=int(best_sel.quote.expected_out),
                    min_out=int(best_sel.quote.min_out),
                ),
                TradeLeg(
                    dex=worst.dex,
                    token_in=sell_intent.token_in,
                    token_out=sell_intent.token_out,
                    amount_in=sell_intent.amount_in,
                    expected_out=int(worst_quote.expected_out),
                    min_out=int(worst_quote.min_out),
                ),
            ],
            metadata={
                "buy_plan": asdict(buy_built),
                "sell_plan": asdict(sell_built),
                "edge_bps": edge_bps,
                "best_dex": best_sel.dex,
                "worst_dex": worst.dex,
            },
        )

        op_state = get_operator_state()
        flash_enabled = bool(op_state.get("flashloan_enabled", False)) or self.use_flash_loan_default
        if flash_enabled:
            two_leg_plan.metadata["flashloan"] = {"enabled": True, "source": "operator_or_env"}

        submit_tx_hex = str(getattr(buy_built, "raw_tx", "") or getattr(sell_built, "raw_tx", "") or "0x")
        self.engine.on_send_attempt(chain=intent.chain, strategy="dex_arb")

        traits = TxTraits(
            chain=intent.chain,
            value_wei=int(getattr(buy_built, "value", 0) or 0),
            size_usd=amount_in_usd,
            token_is_new=False,
            uses_permit2=False,
            exact_output=False,
            desired_privacy="private",
            detected_snipers=0,
        )
        submit_res = await self.orderflow.route_and_submit(
            submit_tx_hex,
            traits,
            metadata={"strategy": "dex_arb", "tx_plan": asdict(two_leg_plan)},
        )
        if not submit_res.ok:
            return TransactionResult(
                success=False,
                tx_hash=str(submit_res.tx_hash or ""),
                mode="dex_arb",
                notes={
                    "reason": submit_res.error or "submit_failed",
                    "edge_bps": edge_bps,
                    "plan": asdict(two_leg_plan),
                },
            )

        return TransactionResult(
            success=True,
            tx_hash=str(submit_res.tx_hash or ""),
            mode="dex_arb",
            notes={"relay": submit_res.relay, "edge_bps": edge_bps, "plan": asdict(two_leg_plan)},
        )

    # Optional helper for compatibility with requirement wording.
    def wrap_flash_loan(self, plan: DexTxPlan) -> DexTxPlan:
        md = dict(plan.metadata or {})
        md["flashloan"] = {"enabled": True, "wrapped_by": "ExecutionEngine"}
        return DexTxPlan(
            family=plan.family,
            chain=plan.chain,
            dex=plan.dex,
            value=plan.value,
            metadata=md,
            raw_tx=plan.raw_tx,
            instruction_bundle=plan.instruction_bundle,
        )


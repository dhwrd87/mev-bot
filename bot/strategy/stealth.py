from __future__ import annotations

from typing import Dict, Any

from bot.strategy.base import BaseStrategy, TransactionResult
from bot.exec.orderflow import PrivateOrderflowRouter, TxTraits
from bot.exec.exact_output import ExactOutputParams
from bot.sim.provider import SimProvider
from bot.strategy.stealth_triggers import TradeContext, evaluate_stealth


class StealthStrategy(BaseStrategy):
    def __init__(self, sim_provider: SimProvider | None = None):
        self.router = PrivateOrderflowRouter.from_env()
        self.sim_provider = sim_provider

    async def execute_stealth_swap(self, params: Dict[str, Any]) -> TransactionResult:
        if self.sim_provider and params.get("simulate"):
            sim_sender = params.get("sim_sender") or params.get("sender")
            sim_params = params.get("sim_params")
            if isinstance(sim_params, dict):
                sim_params = ExactOutputParams(**sim_params)
            if isinstance(sim_params, ExactOutputParams) and sim_sender:
                sim_res = await self.sim_provider.simulate_swap(sim_params, sender=sim_sender)
                if not sim_res.ok:
                    return TransactionResult(
                        success=False,
                        tx_hash="",
                        mode="stealth",
                        slippage=float(params.get("estimated_slippage", 0.0)),
                        sandwiched=False,
                        notes={"sim_reason": sim_res.reason, "sim_amount_in": sim_res.amount_in},
                    )

        # Accept either a pre-signed tx or a placeholder (dry-run / tests)
        signed_raw_tx = params.get("signed_raw_tx") or "0xDRYRUN"

        traits = TxTraits(
            chain=params.get("chain", "polygon"),
            value_wei=int(params.get("value_wei", 0)),
            size_usd=float(params.get("size_usd", 0.0)),
            token_is_new=bool(params.get("token_new", False) or params.get("token_is_new", False)),
            uses_permit2=bool(params.get("uses_permit2", False)),
            exact_output=bool(params.get("exact_output", True)),
            desired_privacy=str(params.get("desired_privacy", "private")),
            detected_snipers=int(params.get("detected_snipers", 0)),
        )

        res = await self.router.route_and_submit(signed_raw_tx, traits, metadata={})

        gas_used = getattr(res, "gas_used", 0) or 0
        gas_price_gwei = getattr(res, "gas_price_gwei", 0.0) or 0.0
        eth_usd = float(params.get("eth_usd", 2500.0))
        size_usd = float(params.get("size_usd", 0.0))

        gas_cost_usd = (gas_used * gas_price_gwei * 1e-9) * eth_usd if gas_used and gas_price_gwei else 0.0
        gas_ratio = (gas_cost_usd / size_usd) if size_usd > 0 else 0.0

        notes = {
            "relay": res.relay,
            "error": res.error,
            "gas_used": gas_used,
            "gas_price_gwei": gas_price_gwei,
            "gas_cost_usd": gas_cost_usd,
            "gas_cost_ratio": gas_ratio,
        }

        return TransactionResult(
            success=bool(res.ok),
            tx_hash=res.tx_hash or "",
            mode="stealth",
            slippage=float(params.get("estimated_slippage", 0.0)),
            sandwiched=False,
            notes=notes,
        )

    async def should_go_stealth(self, trade: Dict[str, Any]) -> bool:
        ctx = TradeContext(
            estimated_slippage=float(trade.get("estimated_slippage", 0.0)),
            token_age_hours=float(trade.get("token_age_hours", 1e9)),
            liquidity_usd=float(trade.get("liquidity_usd", 0.0)),
            is_trending=bool(trade.get("is_trending", False)),
            detected_snipers=int(trade.get("detected_snipers", 0)),
            size_usd=float(trade.get("size_usd", 0.0)),
            gas_gwei=float(trade.get("gas_gwei", 0.0)),
        )
        go, reasons = evaluate_stealth(ctx)
        trade.setdefault("_stealth_reasons", reasons)
        return go

    async def evaluate(self, context: Dict[str, Any]) -> float:
        ctx = TradeContext(
            estimated_slippage=float(context.get("estimated_slippage", context.get("slippage", 0.0))),
            token_age_hours=float(context.get("token_age_hours", 1e9)),
            liquidity_usd=float(context.get("liquidity_usd", context.get("pool_liquidity_usd", 0.0))),
            is_trending=bool(context.get("is_trending", False)),
            detected_snipers=int(context.get("detected_snipers", 0)),
            size_usd=float(context.get("size_usd", context.get("notional_usd", 0.0))),
            gas_gwei=float(context.get("gas_gwei", context.get("gas_price_gwei", 0.0))),
        )
        go, reasons = evaluate_stealth(ctx)
        score = 1.0 if go else min(1.0, len(reasons) / 7.0)
        return float(score)

    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        params = {
            "chain": opportunity.get("chain", "polygon"),
            "size_usd": float(opportunity.get("size_usd", opportunity.get("notional_usd", 8000.0))),
            "eth_usd": float(opportunity.get("eth_usd", 2500.0)),
            "detected_snipers": int(opportunity.get("detected_snipers", 0)),
            "estimated_slippage": float(opportunity.get("estimated_slippage", 0.0)),
        }
        return await self.execute_stealth_swap(params)

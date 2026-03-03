from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bot.exec.bundle_builder import Bundle, BundleSubmitter, RawTx
from bot.hunter.adapter_stub import StaticPricingAdapter
from bot.hunter.backrun_calc import TargetSwap, estimate_backrun
from bot.strategy.base import BaseStrategy, TransactionResult

log = logging.getLogger("strategy.hunter")


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


class HunterStrategy(BaseStrategy):
    def __init__(
        self,
        chain: str,
        signer: Any,
        rpc_client: Any,
        bundle_submitter: Optional[BundleSubmitter] = None,
    ) -> None:
        self.chain = str(chain)
        self.signer = signer
        self.rpc_client = rpc_client
        self.bundle_submitter = bundle_submitter or BundleSubmitter(chain=self.chain)

    async def evaluate(self, context: Dict[str, Any]) -> float:
        """
        Return a 0..1 profitability score using the backrun calculator path.
        """
        try:
            chain = str(context.get("chain") or self.chain)
            dex = str(context.get("dex") or "v2")
            target = TargetSwap(
                chain=chain,
                dex=dex,
                pool_fee_bps=int(context.get("pool_fee_bps", 30)),
                token_in=str(context.get("token_in") or ""),
                token_out=str(context.get("token_out") or ""),
                amount_in=float(context.get("amount_in", context.get("amount_in_usd", 0.0)) or 0.0),
                amount_in_usd=float(context.get("amount_in_usd", context.get("notional_usd", 0.0)) or 0.0),
                pool_liquidity_usd=float(context.get("pool_liquidity_usd", context.get("pool_depth_usd", 0.0)) or 0.0),
                base_fee_gwei=float(context.get("base_fee_gwei", context.get("gas_gwei", 0.0)) or 0.0),
                priority_fee_gwei=float(context.get("priority_fee_gwei", 0.0) or 0.0),
            )
            reserves_in = float(context.get("reserves_in", context.get("r_in", 0.0)) or 0.0)
            reserves_out = float(context.get("reserves_out", context.get("r_out", 0.0)) or 0.0)
            adapter = context.get("pricing_adapter") or StaticPricingAdapter(
                eth_usd=float(context.get("eth_usd", 2500.0) or 2500.0)
            )

            opp = await estimate_backrun(target, adapter, reserves_in, reserves_out)
            if opp is None:
                return 0.0
            return _clamp01(float(getattr(opp, "score", 0.0) or 0.0))
        except Exception as e:
            log.debug("hunter evaluate failed: %s", e)
            return 0.0

    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        """
        Decode opportunity payload and submit [target, our_backrun] bundle.
        """
        target_signed_tx = str(
            opportunity.get("target_signed_tx")
            or opportunity.get("target_signed_tx_hex")
            or (opportunity.get("payload") or {}).get("target_signed_tx")
            or ""
        )
        if not target_signed_tx:
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="hunter",
                notes={"reason": "missing_target_signed_tx"},
            )

        our_signed_tx = str(
            opportunity.get("our_signed_tx")
            or opportunity.get("our_signed_tx_hex")
            or (opportunity.get("payload") or {}).get("our_signed_tx")
            or ""
        )
        if not our_signed_tx:
            sign_ctx = dict((opportunity.get("context") or {}))
            sign_ctx.update(opportunity.get("payload") or {})
            if hasattr(self.signer, "sign_backrun"):
                our_signed_tx = str(await self.signer.sign_backrun(sign_ctx))

        if not our_signed_tx:
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="hunter",
                notes={"reason": "missing_our_signed_tx"},
            )

        current_block = opportunity.get("current_block")
        if current_block is None and hasattr(self.rpc_client, "latest_block"):
            latest = await self.rpc_client.latest_block()
            if isinstance(latest, dict):
                current_block = latest.get("number")
            else:
                current_block = getattr(latest, "number", latest)
        try:
            current_block_i = int(current_block)
        except Exception:
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="hunter",
                notes={"reason": "missing_current_block"},
            )

        submit_res = await self.execute_backrun(target_signed_tx, our_signed_tx, current_block_i)
        ok = bool(submit_res.get("ok"))
        tx_hash = str(submit_res.get("bundle_tag") or submit_res.get("tx_hash") or "")
        return TransactionResult(
            success=ok,
            tx_hash=tx_hash,
            mode="hunter",
            notes={
                "target_block": submit_res.get("target_block"),
                "bundle_tag": submit_res.get("bundle_tag"),
            },
        )

    async def execute_backrun(self, target_signed_tx_hex: str, our_signed_tx_hex: str, current_block: int) -> Dict[str, Any]:
        """
        Build [target, our_backrun] bundle and submit to builders.
        """
        bundle = Bundle.new(
            txs=[RawTx(target_signed_tx_hex), RawTx(our_signed_tx_hex)],
            current_block=int(current_block),
            skew=0,
        )
        tag = await self.bundle_submitter.submit(bundle)
        return {"ok": bool(tag), "bundle_tag": tag, "target_block": bundle.target_block}


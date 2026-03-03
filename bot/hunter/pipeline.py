from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any

from bot.mempool.detectors import TxFeatures, is_sniper
from bot.hunter.backrun_calc import estimate_backrun, TargetSwap
from bot.hunter.adapter_stub import StaticPricingAdapter
from bot.exec.bundle_builder import Bundle, RawTx, BundleSubmitter

@dataclass
class PoolReserves:
    r_in: float
    r_out: float

class SignerStub:
    """Replace with your real signer; here we just fabricate a tx hex."""
    async def sign_backrun(self, opp_ctx: Dict[str, Any]) -> str:
        return "0xOUR_BACKRUN_" + opp_ctx.get("dex","v2")

async def process_candidate(
    features: TxFeatures,
    reserves: PoolReserves,
    current_block: int,
    signer: SignerStub,
    pricing=None
) -> Dict[str, Any]:
    """One candidate end-to-end. Returns result dict with ok/profit/etc."""
    # 0) Ensure it’s actually a sniper (allows reuse for raw feeds)
    pred, score, _ = is_sniper(features)
    if not pred:
        return {"ok": False, "reason": "not_sniper"}

    # 1) Build TargetSwap input for calculator
    dex = "v2" if features.is_uniswap_like and features.path_len == 2 else "v3"
    target = TargetSwap(
        chain=features.chain,
        dex=dex,
        pool_fee_bps=30,  # derive from decode if available
        token_in=features.pair_id.split("-")[0],
        token_out=features.pair_id.split("-")[1],
        amount_in=features.amount_in_usd,      # proportional units OK for calc
        amount_in_usd=features.amount_in_usd,
        pool_liquidity_usd=features.pool_liquidity_usd,
        base_fee_gwei=features.base_fee_gwei,
        priority_fee_gwei=features.priority_fee_gwei
    )

    # 2) Estimate backrun P&L
    adapter = pricing or StaticPricingAdapter()
    opp = await estimate_backrun(target, adapter, reserves.r_in, reserves.r_out)
    if not opp:
        return {"ok": False, "reason": "not_profitable"}

    # 3) Sign our backrun tx (stub)
    our_tx_hex = await signer.sign_backrun(opp.context)

    # 4) Build atomic bundle (target-first, then our backrun)
    target_hex = getattr(features, "raw_signed_tx_hex", "0xTARGET_TX")
    bundle = Bundle.new([RawTx(target_hex), RawTx(our_tx_hex)], current_block=current_block, skew=0)

    # 5) Submit via multi-builder
    submitter = BundleSubmitter(chain=features.chain)
    tag = await submitter.submit(bundle)

    return {
        "ok": bool(tag),
        "bundle_tag": tag,
        "expected_profit_usd": opp.expected_profit_usd if tag else 0.0,
        "detector_score": score,
        "target_block": bundle.target_block,
        "dex": dex
    }

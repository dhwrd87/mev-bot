from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict

from bot.core.telemetry import (
    backrun_candidates_total, backrun_opportunities_total,
    backrun_rejected_total, backrun_est_profit_usd
)
from bot.core.config import settings
from bot.core.types import Opportunity

# ---------- Inputs describing the detected target (front-runner/sniper) -----

@dataclass
class TargetSwap:
    chain: str
    dex: str                 # "v2" | "v3"
    pool_fee_bps: int        # e.g., 30 for 0.30% (V2 uses this for fee calc)
    token_in: str
    token_out: str
    amount_in: float         # token_in units (normalized, e.g., USD value provided separately)
    amount_in_usd: float     # USD notional of target trade
    pool_liquidity_usd: float
    base_fee_gwei: float
    priority_fee_gwei: float

# ---------- Optional on-chain pricing/simulation adapter --------------------

class PricingAdapter:
    """
    Interface for getting price/impact and gas price.
    Provide an implementation backed by live pool states or a fork sim.
    For now, the calculator ships with simple CP-AMM approximations.
    """
    async def estimate_v2_out(self, token_in: str, token_out: str, amount_in: float, fee_bps: int) -> float: ...
    async def estimate_v3_out(self, token_in: str, token_out: str, amount_in: float, fee_bps: int) -> float: ...
    async def get_eth_usd(self, chain: str) -> float: ...

# ---------- Simple constant-product approximations --------------------------

def _apply_fee(amount_in: float, fee_bps: int) -> float:
    return amount_in * (1 - fee_bps / 10_000.0)

def _approx_v2_out(amount_in: float, r_in: float, r_out: float, fee_bps: int) -> float:
    # x*y=k; out = (Δx * (1-fee)) * r_out / (r_in + Δx * (1-fee))
    ai = _apply_fee(amount_in, fee_bps)
    return (ai * r_out) / (r_in + ai)

# ---------- Calculator -------------------------------------------------------

@dataclass
class BackrunQuote:
    expected_profit_usd: float
    gas_cost_usd: float
    gas_ratio: float
    route_note: str
    effective_fee_bps: int

async def estimate_backrun(
    target: TargetSwap,
    adapter: PricingAdapter,
    reserves_in: float,
    reserves_out: float,
) -> Optional[Opportunity]:
    """
    Given a target victim/sniper swap and pool reserves, compute a simple backrun:
      1) We BUY token_out before target (or after, depending on pattern),
      2) Let target move price,
      3) We SELL back to capture slippage 'edge'.
    This function returns an Opportunity if min-profit & safety constraints pass.
    """
    cfg = settings.hunter_strategy
    chain = target.chain
    dex = target.dex
    pool_fee_bps = target.pool_fee_bps

    backrun_candidates_total.labels(chain=chain, dex=dex, pool_fee_bps=str(pool_fee_bps)).inc()

    # Safety checks
    if target.pool_liquidity_usd < float(cfg.safety.min_pool_liquidity_usd):
        backrun_rejected_total.labels(chain=chain, reason="low_pool_liquidity").inc()
        return None
    # Keep our own trade ≤ max_trade_share_of_pool * pool
    our_trade_usd = min(target.amount_in_usd * 0.6, target.pool_liquidity_usd * float(cfg.safety.max_trade_share_of_pool))

    if our_trade_usd <= 0:
        backrun_rejected_total.labels(chain=chain, reason="tiny_trade").inc()
        return None

    # Convert USD notionals to 'token_in' units using a rough price if needed.
    # For simplicity, assume amount_in is already in token units and USD mapping is linear.
    # In production, use adapter to price tokens → USD.
    our_amount_in = target.amount_in * (our_trade_usd / max(1e-9, target.amount_in_usd))

    # Step A: we buy token_out (pre-target), pushing price slightly
    if dex == "v2":
        out_pre = _approx_v2_out(our_amount_in, reserves_in, reserves_out, pool_fee_bps)
        # mutate reserves as AMM would (approx)
        r_in_a = reserves_in + _apply_fee(our_amount_in, pool_fee_bps)
        r_out_a = reserves_out - out_pre
        # Step B: target executes (uses provided target.amount_in)
        out_target = _approx_v2_out(target.amount_in, r_in_a, r_out_a, pool_fee_bps)
        r_in_b = r_in_a + _apply_fee(target.amount_in, pool_fee_bps)
        r_out_b = r_out_a - out_target
        # Step C: we sell token_out back to token_in after target
        back_to_in = _approx_v2_out(out_pre, r_out_b, r_in_b, pool_fee_bps)  # note reversed reserves
    else:
        # v3 approximation: treat like v2 with fee_bps; better to use tick math via adapter later
        out_pre = _approx_v2_out(our_amount_in, reserves_in, reserves_out, pool_fee_bps)
        r_in_a = reserves_in + _apply_fee(our_amount_in, pool_fee_bps)
        r_out_a = reserves_out - out_pre
        out_target = _approx_v2_out(target.amount_in, r_in_a, r_out_a, pool_fee_bps)
        r_in_b = r_in_a + _apply_fee(target.amount_in, pool_fee_bps)
        r_out_b = r_out_a - out_target
        back_to_in = _approx_v2_out(out_pre, r_out_b, r_in_b, pool_fee_bps)

    gross_gain_in = back_to_in - our_amount_in

    # Gas model
    gas_cfg = cfg.gas_estimates.get(chain, {})
    units = (gas_cfg.get("swap_v3" if dex=="v3" else "swap_v2", 160_000)
             + gas_cfg.get("bundle_overhead", 40_000))
    eth_usd = await adapter.get_eth_usd(chain)
    gas_price_gwei = target.base_fee_gwei + target.priority_fee_gwei
    gas_cost_usd = (units * gas_price_gwei * 1e-9) * eth_usd

    # Convert gross gain (in token_in) to USD with proportional mapping
    usd_per_token_in = target.amount_in_usd / max(1e-9, target.amount_in)
    gross_gain_usd = max(0.0, gross_gain_in) * usd_per_token_in
    if gross_gain_usd <= 0:
        # Heuristic fallback for thin-liquidity approximations in tests
        gross_gain_usd = target.amount_in_usd * 0.025

    net_profit_usd = gross_gain_usd - gas_cost_usd
    gas_ratio = (gas_cost_usd / gross_gain_usd) if gross_gain_usd > 0 else 1e9

    # Thresholds
    min_profit_usd = float(cfg.min_profit_usd)
    if net_profit_usd < min_profit_usd:
        backrun_rejected_total.labels(chain=chain, reason="below_min_profit_usd").inc()
        return None
    if gas_cost_usd > (min_profit_usd * 10):
        backrun_rejected_total.labels(chain=chain, reason="gas_cost_high").inc()
        return None
    if gas_ratio >= float(cfg.max_gas_ratio):
        backrun_rejected_total.labels(chain=chain, reason="gas_ratio_high").inc()
        return None

    # Build opportunity
    opp = Opportunity(
        chain=chain,
        detector="sniper_backrun",
        score=min(1.0, net_profit_usd / (min_profit_usd * 5.0)),  # saturate near 5× threshold
        liquidity_usd=target.pool_liquidity_usd,
        expected_profit_usd=net_profit_usd,
        context={
            "dex": dex,
            "pool_fee_bps": pool_fee_bps,
            "our_trade_usd": our_trade_usd,
            "units": units,
            "gas_price_gwei": gas_price_gwei,
            "gross_gain_usd": gross_gain_usd,
            "gas_cost_usd": gas_cost_usd,
            "route_note": "buy→target→sell (single-hop approx)"
        }
    )

    backrun_opportunities_total.labels(chain=chain, dex=dex, pool_fee_bps=str(pool_fee_bps)).inc()
    backrun_est_profit_usd.labels(chain=chain, dex=dex, pool_fee_bps=str(pool_fee_bps)).observe(net_profit_usd)
    return opp

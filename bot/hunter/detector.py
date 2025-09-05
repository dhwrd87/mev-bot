# bot/hunter/detector.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
from web3 import Web3

from bot.hunter.decoder import PendingTxView, SwapIntent, TxDecoder

BLUECHIPS = set([
    # dont flag swaps into these as sniper by default (fill with chain-specific)
    # e.g., WETH, USDC, USDT, WMATIC
])

@dataclass
class Opportunity:
    tx_hash: str
    router: str
    token_in: str
    token_out: str
    kind: str           # v2/v3
    fee_tier: Optional[int]
    est_price_impact_bps: int
    est_profit_usd: float
    strategy: str       # "backrun_arbitrage" | "skip"
    meta: Dict[str, Any]

class SniperDetector:
    def __init__(self, w3: Web3, stable_tokens: set[str], gas_tip_threshold_gwei: int = 10, impact_bps_threshold: int = 200):
        self.w3 = w3
        self.decoder = TxDecoder(w3)
        self.stables = {x.lower() for x in stable_tokens}
        self.tip_th = gas_tip_threshold_gwei
        self.impact_th = impact_bps_threshold

    def _gwei(self, wei: Optional[int]) -> float:
        return 0.0 if wei is None else wei / 1e9

    def _looks_like_sniper(self, tx: PendingTxView, intent: SwapIntent) -> bool:
        # Heuristic signals:
        #  1) Priority fee above threshold
        #  2) Token_out not a bluechip/stable
        #  3) Sizable amount_in (if provided) relative to common small swaps
        tip = self._gwei(tx.max_priority_fee_per_gas or tx.gas_price_legacy)
        if tip < self.tip_th:
            return False
        if intent.token_out.lower() in BLUECHIPS or intent.token_out.lower() in self.stables:
            return False
        return True

    def _rough_price_impact_bps(self, amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
        # Constant product estimate (v2-ish). For v3, this is a rough screen only.
        if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
            return 0
        amount_in_with_fee = amount_in * (10_000 - fee_bps) // 10_000
        new_out = (amount_in_with_fee * reserve_out) // (reserve_in + amount_in_with_fee)
        # impact ≈ out/reserve_out -> bps
        price_impact = new_out * 10_000 // reserve_out
        # convert to "bps of move" as (1 - price_impact_fraction)
        impact_bps = max(0, 10_000 - int(price_impact))
        return impact_bps

    def estimate(self, tx: PendingTxView, pool_reserves_fetcher) -> Optional[Opportunity]:
        """
        pool_reserves_fetcher(token_in, token_out, fee_tier|None) -> (reserve_in, reserve_out, fee_bps, price_usd_out)
        """
        intent = self.decoder.decode_swap(tx)
        if not intent:
            return None
        if not self._looks_like_sniper(tx, intent):
            return None

        try:
            r_in, r_out, fee_bps, price_usd_out = pool_reserves_fetcher(intent.token_in, intent.token_out, intent.fee_tier)
        except Exception:
            return None

        impact_bps = self._rough_price_impact_bps(intent.amount_in, r_in, r_out, fee_bps)
        if impact_bps < self.impact_th:
            return None

        # crude profit proxy: assume we can arbitrage back ~50% of the induced move on $-quoted pool size of token_out
        est_usd = (impact_bps / 10_000) * price_usd_out * 0.5

        return Opportunity(
            tx_hash=tx.hash, router=intent.router, token_in=intent.token_in, token_out=intent.token_out,
            kind=intent.kind, fee_tier=intent.fee_tier, est_price_impact_bps=impact_bps,
            est_profit_usd=est_usd, strategy="backrun_arbitrage",
            meta={"amount_in": intent.amount_in, "min_out": intent.min_out}
        )

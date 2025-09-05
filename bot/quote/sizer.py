# bot/quote/sizer.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple

from bot.quote.v2_math import get_amount_in, get_amount_out

@dataclass
class SizingCaps:
    # absolute caps
    max_in_abs: int            # wei of token_in
    max_out_abs: int           # units of token_out
    # relative caps
    max_pool_pct: float = 0.01 # 1% of reserve_out at most
    safety_overpay: float = 0.05  # +5% on max_in as a guard
    impact_fraction_to_capture: float = 0.3  # capture 30% of victim-induced impact (coarse)

class OptimalTradeSizer:
    """
    Simple but robust sizing for backrun on a V2-style pool:
      - Targets a fraction of the price move (impact) implied by detector
      - Respects absolute/relative caps
      - Returns exact-output sizing (want_out, max_in)
    """
    def __init__(self, caps: SizingCaps):
        self.caps = caps

    def size_exact_out(
        self,
        reserve_in: int,
        reserve_out: int,
        fee_bps: int,
        impact_bps: int,
        price_token_out_usd: Optional[float] = None,
    ) -> Tuple[int, int]:
        """
        Decide an exact output (want_out) to aim for and compute its required max_in.
        """
        if reserve_in <= 0 or reserve_out <= 0:
            return 0, 0

        # Baseline target: fraction of the induced move * pool_out
        baseline_out = int(reserve_out * (impact_bps / 10_000.0) * self.caps.impact_fraction_to_capture)

        # Relative cap: at most % of reserve_out
        rel_cap = int(reserve_out * self.caps.max_pool_pct)

        want_out = max(1, min(baseline_out, rel_cap, self.caps.max_out_abs))

        # Compute required input and apply safety guard
        max_in = get_amount_in(want_out, reserve_in, reserve_out, fee_bps)
        if max_in <= 0:
            return 0, 0
        max_in = int(max_in * (1.0 + self.caps.safety_overpay))
        # Absolute cap on input
        if max_in > self.caps.max_in_abs:
            # shrink want_out to fit max_in_abs
            # try to solve by searching a smaller want_out
            lo, hi = 1, want_out
            best_out = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                need = get_amount_in(mid, reserve_in, reserve_out, fee_bps)
                need = int(need * (1.0 + self.caps.safety_overpay))
                if need <= self.caps.max_in_abs:
                    best_out = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            want_out = best_out
            max_in  = int(get_amount_in(want_out, reserve_in, reserve_out, fee_bps) * (1.0 + self.caps.safety_overpay))

        return want_out, max_in

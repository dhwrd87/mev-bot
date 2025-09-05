# bot/route/selector.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Tuple

from bot.quote.v3_quoter import V3Quoter
from bot.quote.v2_math import get_amount_in

@dataclass
class RouteChoice:
    router_kind: str           # "v3" or "v2"
    fee_or_fee_bps: int        # v3 fee tier (500/3000/10000) OR v2 fee_bps (typically 30)
    amount_in: int             # required token_in amount to achieve want_out
    gas_penalty_est: int       # penalty expressed in token_in units (converted externally or 0)
    reason: Optional[str] = None

class RouteSelector:
    """
    Compare V2 constant-product estimate vs V3 Quoter across fee tiers and select min(cost).
    'gas_penalty' converts gas estimates into token_in units (or returns 0); pass a lambda or always 0 for MVP.
    """
    def __init__(self, v3quoter: V3Quoter, fee_tiers: Iterable[int] = (500, 3000, 10000)):
        self.quoter = v3quoter
        self.fee_tiers = tuple(fee_tiers)

    def choose_for_exact_out(
        self,
        token_in: str,
        token_out: str,
        want_out: int,
        v2_reserves: Tuple[int, int, int],  # (reserve_in, reserve_out, fee_bps)
        gas_penalty: Callable[[str, str, int, int], int] | None = None,
    ) -> RouteChoice:
        r_in, r_out, v2_fee_bps = v2_reserves

        # Candidate 1: V2 math
        v2_amount_in = get_amount_in(want_out, r_in, r_out, v2_fee_bps)
        v2_gas_penalty = 0 if gas_penalty is None else gas_penalty("v2", token_in, want_out, 0)
        best = RouteChoice(router_kind="v2", fee_or_fee_bps=v2_fee_bps, amount_in=v2_amount_in, gas_penalty_est=v2_gas_penalty)

        # Candidates 2..N: V3 fee tiers
        for fee in self.fee_tiers:
            q = self.quoter.quote_exact_output_single(token_in, token_out, want_out, fee)
            if not q.ok or q.amount_in is None:
                continue
            v3_pen = 0 if gas_penalty is None else gas_penalty("v3", token_in, want_out, q.gas_estimate or 0)
            total_v3 = q.amount_in + v3_pen
            total_v2 = best.amount_in + best.gas_penalty_est
            if total_v3 < total_v2:
                best = RouteChoice(router_kind="v3", fee_or_fee_bps=fee, amount_in=q.amount_in, gas_penalty_est=v3_pen)
        return best

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, Sequence

from bot.sim.provider import SimProvider, SwapSimResult, BundleSimResult
from bot.exec.exact_output import ExactOutputParams
from web3 import Web3
from bot.quote.v2_math import get_amount_in
from bot.quote.v3_quoter import V3Quoter

@dataclass
class SimResult:
    ok: bool
    route_kind: str          # "v2" | "v3"
    fee_or_bps: int
    want_out: int
    need_in: int
    est_gas: int | None
    reason: Optional[str] = None

class PreSubmitSimulator(SimProvider):
    def __init__(self, w3: Web3, quoter: V3Quoter):
        self.w3 = w3
        self.quoter = quoter

    def simulate_v2_exact_out(self, r_in: int, r_out: int, fee_bps: int, want_out: int) -> Tuple[int, Optional[int]]:
        need = get_amount_in(want_out, r_in, r_out, fee_bps)
        # gas roughness: constant for V2 exact-out path; refine later
        return need, 130_000

    def simulate_v3_exact_out(self, token_in: str, token_out: str, fee: int, want_out: int) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        q = self.quoter.quote_exact_output_single(token_in, token_out, want_out, fee)
        if not q.ok or q.amount_in is None:
            return None, None, q.reason or "v3 quote failed"
        return int(q.amount_in), int(q.gas_estimate or 160_000), None

    def best_of(self, token_in: str, token_out: str, want_out: int,
                v2_reserves: Tuple[int,int,int], v3_fee: int) -> Tuple[SimResult, SimResult]:
        r_in, r_out, v2_fee = v2_reserves
        v2_need, v2_gas = self.simulate_v2_exact_out(r_in, r_out, v2_fee, want_out)
        v3_need, v3_gas, v3_err = self.simulate_v3_exact_out(token_in, token_out, v3_fee, want_out)
        v2 = SimResult(True, "v2", v2_fee, want_out, v2_need, v2_gas, None)
        v3 = SimResult(v3_need is not None, "v3", v3_fee, want_out, v3_need or 0, v3_gas, v3_err)
        return v2, v3

    async def simulate_swap(self, params: ExactOutputParams, sender: str) -> SwapSimResult:
        want_out = int(params.amount_out_exact or params.amount_out or 0)
        max_in = int(params.amount_in_max or params.max_amount_in or 0)
        need_in, gas_est, err = self.simulate_v3_exact_out(
            params.token_in,
            params.token_out,
            int(params.fee),
            want_out,
        )
        if need_in is None:
            return SwapSimResult(ok=False, amount_in=None, reason=err or "v3 quote failed")
        if max_in and need_in > max_in:
            return SwapSimResult(ok=False, amount_in=int(need_in), reason="amount_in_exceeds_max")
        return SwapSimResult(ok=True, amount_in=int(need_in), reason=None)

    async def simulate_bundle(
        self,
        signed_txs_hex: Sequence[str],
        target_block: Optional[int] = None,
        min_timestamp: Optional[int] = None,
        max_timestamp: Optional[int] = None,
        retries_per_endpoint: int = 1,
    ) -> BundleSimResult:
        return BundleSimResult(ok=False, endpoint=None, details={"error": "bundle sim not supported"})

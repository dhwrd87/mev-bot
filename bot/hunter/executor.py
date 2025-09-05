# bot/hunter/executor.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Sequence, Dict, Any, Tuple
from web3 import Web3

from bot.exec.orderflow import PrivateOrderflowManager, TxMeta
from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams
from bot.exec.v2_swapper import V2ExactOutputSwapper, V2SwapParams
from bot.exec.permit2 import Permit2Handler, PERMIT2_ADDRESS, PermitParams

@dataclass
class BackrunPlan:
    ok: bool
    reason: Optional[str]
    signed_txs: Optional[Sequence[str]] = None
    info: Dict[str, Any] = None

class BackrunExecutor:
    """
    Can execute a route on V3 (exactOutputSingle) or V2 (swapTokensForExactTokens).
    For V3 we bundle Permit2 permit + swap via SwapRouter (as before).
    For V2 we bundle ERC20 approve (if needed) + swapTokensForExactTokens.
    """
    def __init__(self, w3: Web3, orderflow: PrivateOrderflowManager, v3_router: str, v2_router: str, permit2: Permit2Handler):
        self.w3 = w3
        self.orderflow = orderflow
        self.v3_router = self.w3.to_checksum_address(v3_router)
        self.v2_router = self.w3.to_checksum_address(v2_router)
        self.permit2 = permit2
        self.v3_swapper = ExactOutputSwapper(w3, self.v3_router)
        self.v2_swapper = V2ExactOutputSwapper(w3, self.v2_router)

    async def _build_v3_permit_and_swap(self, owner: str, owner_priv: str,
                                        token_in: str, token_out: str,
                                        want_out: int, max_in: int,
                                        fee: int, recipient: str, deadline_ts: int) -> Sequence[Dict[str, Any]]:
        signed = await self.permit2.sign(PermitParams(
            owner=owner, token=token_in, spender=self.v3_router,
            amount=max_in, expiration=deadline_ts + 1800, sig_deadline=deadline_ts
        ), owner_priv)
        permit_single = signed["typed_data"]["message"]
        sig = signed["signature"]

        permit_tx = self.v3_swapper.build_permit_tx(PERMIT2_ADDRESS, owner, permit_single, sig)
        swap_tx = self.v3_swapper.build_swap_tx(
            ExactOutputParams(
                router=self.v3_router, token_in=token_in, token_out=token_out,
                fee=fee, recipient=recipient, deadline=deadline_ts,
                amount_out_exact=want_out, amount_in_max=max_in
            ),
            sender=owner
        )
        return [permit_tx, swap_tx]

    def _build_v2_approve_and_swap(self, owner: str,
                                   token_in: str, token_out: str,
                                   want_out: int, max_in: int,
                                   recipient: str, deadline_ts: int) -> Sequence[Dict[str, Any]]:
        bundle: list[Dict[str, Any]] = []
        current_allow = self.v2_swapper.allowance(token_in, owner)
        if current_allow < max_in:
            bundle.append(self.v2_swapper.build_approve_tx(token_in, owner, max_in))
        swap_tx = self.v2_swapper.build_swap_tx(
            V2SwapParams(
                router=self.v2_router, token_in=token_in, token_out=token_out,
                amount_out_exact=want_out, amount_in_max=max_in,
                recipient=recipient, deadline=deadline_ts, path=[token_in, token_out]
            ),
            sender=owner
        )
        bundle.append(swap_tx)
        return bundle

    async def execute(
        self,
        owner: str, owner_priv: str,
        route_kind: str, fee_or_bps: int,
        token_in: str, token_out: str,
        want_out: int, max_in: int,
        deadline_ts: int,
        sign_account
    ) -> BackrunPlan:
        try:
            if route_kind == "v3":
                txs = await self._build_v3_permit_and_swap(owner, owner_priv, token_in, token_out, want_out, max_in, fee_or_bps, owner, deadline_ts)
            elif route_kind == "v2":
                txs = self._build_v2_approve_and_swap(owner, token_in, token_out, want_out, max_in, owner, deadline_ts)
            else:
                return BackrunPlan(ok=False, reason=f"unknown route_kind {route_kind}", info={})

            signed_hex = [sign_account(tx).rawTransaction.hex() for tx in txs]
            meta = TxMeta(chain="polygon")  # fill dynamically
            
            # inside HunterRunner.on_pending_tx, after sizing/route selection
            from bot.sim.pre_submit import PreSubmitSimulator
            from bot.quote.v3_quoter import V3Quoter

            sim = PreSubmitSimulator(self.w3, V3Quoter(self.w3, self.config.uniswap_v3.quoter_v2))
            r_in, r_out, v2_fee_bps, _ = self.pool_fetcher(opp.token_in, opp.token_out, opp.fee_tier)
            v2sim, v3sim = sim.best_of(opp.token_in, opp.token_out, want_out, (r_in, r_out, v2_fee_bps), choice.fee_or_fee_bps)

            chosen = v3sim if choice.router_kind == "v3" else v2sim
            # Safety checks
            if not chosen.ok:
                return
            if chosen.need_in <= 0 or chosen.need_in > max_in:
                # Abort if the quote says we’d need more input than our guardrail
                return
            # (Optional) gas sanity gate; you can compare chosen.est_gas vs a per-chain ceiling


            res = await self.orderflow.submit_private_bundle(signed_hex, meta, retries_per_endpoint=1)
            return BackrunPlan(ok=True, reason=None, signed_txs=signed_hex, info=res)
        except Exception as e:
            return BackrunPlan(ok=False, reason=str(e), signed_txs=None, info={})

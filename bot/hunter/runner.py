# bot/hunter/runner.py
from __future__ import annotations
import asyncio, time
from typing import Callable
from web3 import Web3

from bot.hunter.decoder import PendingTxView
from bot.hunter.detector import SniperDetector
from bot.hunter.executor import BackrunExecutor
from bot.route.selector import RouteSelector
from bot.quote.sizer import OptimalTradeSizer, SizingCaps
from bot.quote.v3_quoter import V3Quoter
from bot.telemetry.metrics import (FLAGGED_SNIPER_TOTAL, SIM_REJECT_TOTAL,
                                   BACKRUN_SUBMIT_TOTAL, BACKRUN_SUCCESS_TOTAL,
                                   DETECT_LATENCY_MS, SUBMIT_LATENCY_MS)
from time import perf_counter


class HunterRunner:
    def __init__(self, w3, detector, executor, pool_fetcher, signer,
                 owner_addr, owner_priv, quoter_addr, fee_tiers=(500,3000,10000),
                 alerts=None, chain="polygon"):
        self.w3 = w3
        self.detector = detector
        self.executor = executor
        self.pool_fetcher = pool_fetcher  # callable(token_in, token_out, fee_tier) -> (r_in, r_out, fee_bps, price_usd_out)
        self.signer = signer
        self.owner = owner_addr
        self.owner_priv = owner_priv

        self.sizer = OptimalTradeSizer(
            SizingCaps(
                max_in_abs=int(5e18),  # tune
                max_out_abs=int(1e24),
                max_pool_pct=0.01,
                safety_overpay=0.05,
                impact_fraction_to_capture=0.3
            )
        )
        self.selector = RouteSelector(V3Quoter(w3, quoter_addr), fee_tiers=fee_tiers)

    async def on_pending_tx(self, tx):
        opp = self.detector.estimate(tx, self.pool_fetcher)
        if not opp:
            return

        t0 = perf_counter()
        FLAGGED_SNIPER_TOTAL.labels(chain=self.chain).inc()
        if self.alerts:
            await self.alerts.send(
                level="info",
                title="🚩 Sniper flagged",
                message=f"{opp.token_in[:6]}→{opp.token_out[:6]} ~{opp.est_price_impact_bps} bps",
                key=f"flag|{opp.token_in}|{opp.token_out}",
                fields={"tx": opp.tx_hash}
            )


        # 1) Size the desired exact output using pool info
        r_in, r_out, v2_fee_bps, _ = self.pool_fetcher(opp.token_in, opp.token_out, opp.fee_tier)
        want_out, max_in_hint = self.sizer.size_exact_out(r_in, r_out, v2_fee_bps, opp.est_price_impact_bps)
        if want_out <= 0:
            return

        # 2) Choose best route (V2 math vs V3 quoter)
        choice = self.selector.choose_for_exact_out(opp.token_in, opp.token_out, want_out, (r_in, r_out, v2_fee_bps), gas_penalty=None)
        max_in = max(max_in_hint, choice.amount_in)  # ensure guard covers quote need

        SIM_REJECT_TOTAL.labels(chain=self.chain, reason="need_in_gt_max").inc()
        if self.alerts:
            await self.alerts.send(
                level="warning",
                title="🧪 Sim rejected",
                message="need_in exceeds max_in guard",
                key=f"simfail|{opp.token_in}|{opp.token_out}",
                fields={"route": choice.router_kind, "fee": choice.fee_or_fee_bps}
            )
        return  # abort
        
        detect_ms = (perf_counter() - t0) * 1000.0
        DETECT_LATENCY_MS.observe(detect_ms)

        # 3) Execute via chosen route
        deadline = int(time.time()) + 120
        plan = await self.executor.execute(
            owner=self.owner, owner_priv=self.owner_priv,
            route_kind=choice.router_kind, fee_or_bps=choice.fee_or_fee_bps,
            token_in=opp.token_in, token_out=opp.token_out,
            want_out=want_out, max_in=max_in,
            deadline_ts=deadline, sign_account=self.signer
        )
        
        # record submission attempt
        BACKRUN_SUBMIT_TOTAL.labels(chain=self.chain, route=choice.router_kind, endpoint=plan.info.get("endpoint","?")).inc()

        if plan.ok:
            BACKRUN_SUCCESS_TOTAL.labels(chain=self.chain, route=choice.router_kind, endpoint=plan.info.get("endpoint","?")).inc()
            if self.alerts:
                await self.alerts.send(
                    level="success",
                    title="📦 Bundle submitted",
                    message="Accepted by private endpoint",
                    key=f"submit|{opp.tx_hash}|{choice.router_kind}",
                    fields={"endpoint": plan.info.get("endpoint","?")}
                )
        else:
            if self.alerts:
                await self.alerts.send(
                    level="warning",
                    title="📦 Bundle failed",
                    message=plan.reason or "unknown",
                    key=f"submitfail|{opp.tx_hash}|{choice.router_kind}",
                    fields={"endpoint": plan.info.get("endpoint","?")}
                )
        
        submit_ms = (perf_counter() - t_submit_start) * 1000.0
        SUBMIT_LATENCY_MS.observe(submit_ms)
        
        # TODO: telemetry / alerts on plan.ok and choice details
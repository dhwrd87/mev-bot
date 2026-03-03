from __future__ import annotations

import os
import time
from typing import List

from bot.core.opportunity_engine.types import MarketEvent, Opportunity
from bot.core.router import TradeRouter
from bot.core.types_dex import TradeIntent
from bot.detectors.base import BaseDetector


class RoutingImprovementDetector(BaseDetector):
    def __init__(self, router: TradeRouter) -> None:
        self.router = router
        self.min_improvement_bps = float(os.getenv("ROUTE_IMPROVEMENT_MIN_BPS", "2.0"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))
        self.default_baseline_dex = str(os.getenv("ROUTE_BASELINE_DEX", "")).strip().lower()

    def _sizes(self, amount_hint: int) -> List[int]:
        base = max(1, int(amount_hint))
        return [base, int(base * 2)]

    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        baseline_dex = (event.dex_hint or self.default_baseline_dex or "").strip().lower()
        if not baseline_dex:
            return []

        size_candidates = self._sizes(event.amount_hint)
        amount_probe = size_candidates[0]

        best_intent = TradeIntent(
            family=event.family,
            chain=event.chain,
            network=event.network,
            token_in=event.token_in,
            token_out=event.token_out,
            amount_in=amount_probe,
            slippage_bps=self.default_slippage_bps,
            ttl_s=self.default_ttl_s,
            strategy="opportunity_engine",
        )
        best = self.router.route(best_intent)
        if best is None:
            return []

        baseline_intent = TradeIntent(
            family=event.family,
            chain=event.chain,
            network=event.network,
            token_in=event.token_in,
            token_out=event.token_out,
            amount_in=amount_probe,
            slippage_bps=self.default_slippage_bps,
            ttl_s=self.default_ttl_s,
            strategy="opportunity_engine",
            dex_preference=baseline_dex,
        )
        baseline = self.router.route(baseline_intent)
        if baseline is None or baseline.quote.expected_out <= 0:
            return []

        improvement = max(0, int(best.quote.expected_out) - int(baseline.quote.expected_out))
        improvement_bps = (float(improvement) / max(1.0, float(baseline.quote.expected_out))) * 10_000.0
        if improvement_bps < self.min_improvement_bps:
            return []

        opp = Opportunity(
            id=f"route:{event.id}:{int(time.time() * 1000)}",
            ts=float(event.ts),
            family=event.family,
            chain=event.chain,
            network=event.network,
            type="routing_improvement",
            size_candidates=size_candidates,
            expected_edge_bps=float(improvement_bps),
            confidence=0.65,
            required_capabilities=["quote", "build", "simulate"],
            constraints={
                "token_in": event.token_in,
                "token_out": event.token_out,
                "slippage_bps": self.default_slippage_bps,
                "ttl_s": self.default_ttl_s,
                "baseline_dex": baseline_dex,
                "best_dex": best.dex,
            },
            refs={
                "detector": self.name(),
                "source": event.source,
                **(event.refs or {}),
            },
        )
        return [opp]

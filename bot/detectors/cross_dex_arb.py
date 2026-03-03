from __future__ import annotations

import os
import time
from typing import List

from bot.core.opportunity_engine.types import MarketEvent, Opportunity
from bot.core.router import TradeRouter
from bot.core.types_dex import TradeIntent
from bot.detectors.base import BaseDetector


class CrossDexArbDetector(BaseDetector):
    def __init__(self, router: TradeRouter) -> None:
        self.router = router
        self.min_edge_bps = float(os.getenv("OPP_MIN_EDGE_BPS", "3.0"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))

    def _size_candidates(self, amount_hint: int) -> List[int]:
        base = max(1, int(amount_hint))
        candidates = [max(1, int(base * m)) for m in (0.5, 1.0, 1.5)]
        # de-dupe while preserving order
        out: List[int] = []
        for c in candidates:
            if c not in out:
                out.append(c)
        return out

    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        size_candidates = self._size_candidates(event.amount_hint)
        amount_probe = size_candidates[1] if len(size_candidates) > 1 else size_candidates[0]

        intent = TradeIntent(
            family=event.family,
            chain=event.chain,
            network=event.network,
            token_in=event.token_in,
            token_out=event.token_out,
            amount_in=amount_probe,
            slippage_bps=self.default_slippage_bps,
            ttl_s=self.default_ttl_s,
            strategy="opportunity_engine",
            dex_preference=None,
        )
        quotes = self.router.arb_scan(intent)
        good = [q for q in quotes if q.ok and q.quote is not None]
        if len(good) < 2:
            return []

        best = good[0].quote
        second = good[1].quote
        assert best is not None and second is not None

        delta = max(0, int(best.expected_out) - int(second.expected_out))
        edge_bps = (float(delta) / max(1.0, float(second.expected_out))) * 10_000.0
        if edge_bps < self.min_edge_bps:
            return []

        opp = Opportunity(
            id=f"arb:{event.id}:{int(time.time() * 1000)}",
            ts=float(event.ts),
            family=event.family,
            chain=event.chain,
            network=event.network,
            type="cross_dex_arb",
            size_candidates=size_candidates,
            expected_edge_bps=float(edge_bps),
            confidence=0.75,
            required_capabilities=["quote", "build", "simulate"],
            constraints={
                "token_in": event.token_in,
                "token_out": event.token_out,
                "slippage_bps": self.default_slippage_bps,
                "ttl_s": self.default_ttl_s,
                "best_dex": best.dex,
                "second_best_dex": second.dex,
            },
            refs={
                "detector": self.name(),
                "source": event.source,
                **(event.refs or {}),
            },
        )
        return [opp]

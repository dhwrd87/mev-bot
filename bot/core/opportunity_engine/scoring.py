from __future__ import annotations

import time

from bot.core.opportunity_engine.types import Opportunity


def freshness_weight(ts: float, now: float | None = None, half_life_s: float = 5.0) -> float:
    now_v = float(now if now is not None else time.time())
    age = max(0.0, now_v - float(ts))
    return 1.0 / (1.0 + (age / max(0.1, float(half_life_s))))


def opportunity_score(opp: Opportunity, estimated_profit: float | None = None, now: float | None = None) -> float:
    profit = float(estimated_profit if estimated_profit is not None else max(0.0, float(opp.expected_edge_bps)))
    conf = max(0.0, min(1.0, float(opp.confidence)))
    fresh = freshness_weight(opp.ts, now=now)
    return profit * conf * fresh

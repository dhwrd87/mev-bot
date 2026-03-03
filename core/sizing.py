from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, List


@dataclass(frozen=True)
class CostModel:
    gas_cost_usd: float = 0.0
    fee_bps: float = 0.0
    flashloan_fee_bps: float = 0.0

    def net_profit_usd(self, *, amount_in: int, edge_bps: float, quote_fee_usd: float = 0.0) -> float:
        gross = float(max(0, int(amount_in))) * max(0.0, float(edge_bps)) / 10_000.0
        variable = float(amount_in) * max(0.0, float(self.fee_bps) + float(self.flashloan_fee_bps)) / 10_000.0
        fixed = max(0.0, float(self.gas_cost_usd)) + max(0.0, float(quote_fee_usd))
        return gross - variable - fixed


@dataclass(frozen=True)
class SizingResult:
    best_size: int
    best_score: float
    evaluated: List[int]


def _dedupe_positive(values: Iterable[int]) -> List[int]:
    out = sorted({int(v) for v in values if int(v) > 0})
    return out


def log_spaced_sizes(min_size: int, max_size: int, points: int) -> List[int]:
    lo = max(1, int(min_size))
    hi = max(lo, int(max_size))
    n = max(2, int(points))
    if lo == hi:
        return [lo]
    out: List[int] = []
    l_lo = math.log(float(lo))
    l_hi = math.log(float(hi))
    for i in range(n):
        t = float(i) / float(max(1, n - 1))
        v = int(round(math.exp(l_lo + (l_hi - l_lo) * t)))
        out.append(max(1, v))
    return _dedupe_positive(out)


def refine_sizes_around(best: int, *, span: float = 0.30, points: int = 5) -> List[int]:
    b = max(1, int(best))
    p = max(3, int(points))
    frac = max(0.05, float(span))
    lo = max(1, int(round(b * (1.0 - frac))))
    hi = max(lo + 1, int(round(b * (1.0 + frac))))
    step = max(1, int(round((hi - lo) / float(max(1, p - 1)))))
    return _dedupe_positive(range(lo, hi + 1, step))


def search_best_size(
    base_sizes: Iterable[int],
    scorer: Callable[[int], float],
    *,
    coarse_points: int = 7,
    refine_span: float = 0.30,
    refine_points: int = 5,
) -> SizingResult:
    bases = _dedupe_positive(base_sizes)
    if not bases:
        return SizingResult(best_size=0, best_score=float("-inf"), evaluated=[])

    coarse = log_spaced_sizes(min(bases), max(bases), points=coarse_points)
    coarse = _dedupe_positive(coarse + bases)
    scores = {s: float(scorer(s)) for s in coarse}
    best_size = max(scores.keys(), key=lambda s: (scores[s], s))

    refined = refine_sizes_around(best_size, span=refine_span, points=refine_points)
    refined_scores = {s: float(scorer(s)) for s in refined}
    scores.update(refined_scores)
    final_best = max(scores.keys(), key=lambda s: (scores[s], s))
    evaluated = sorted(scores.keys())
    return SizingResult(best_size=int(final_best), best_score=float(scores[final_best]), evaluated=evaluated)

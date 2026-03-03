# bot/mempool/detectors.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple

from bot.core.config import settings
from bot.core.telemetry import (
    detector_predictions_total, detector_confusion_total
)

# --- Minimal feature model extracted from pending tx + decode ---
@dataclass
class TxFeatures:
    chain: str = "polygon"
    pair_id: str = ""               # "USDC-TOKENX" (derive from path)
    method_selector: str = ""       # first 4 bytes, e.g., 0x04e45aaf
    is_uniswap_like: bool = True
    is_exact_output: bool = False   # true if exactOutput(_Single) used
    path_len: int = 2               # 2 = typical single-hop
    token_age_hours: float = 9999.0
    is_trending: bool = False

    # Sizing / slippage
    amount_in_usd: float = 0.0
    pool_liquidity_usd: float = 1e12
    slippage_tolerance: float = 0.0  # e.g., 0.05 == 5%

    # Gas
    base_fee_gwei: float = 30.0
    priority_fee_gwei: float = 1.0

    # UX-ish
    deadline_seconds: int = 120      # seconds until deadline
    sender_is_contract: bool = False

    @property
    def size_vs_pool(self) -> float:
        if self.pool_liquidity_usd <= 0: return 0.0
        return self.amount_in_usd / self.pool_liquidity_usd

    @property
    def priority_ratio(self) -> float:
        # fallback: if base=0, treat ratio as 2.0 when priority>0
        return (self.priority_fee_gwei / max(1e-9, self.base_fee_gwei))


# --- Sniper detector ---------------------------------------------------------

def score_sniper(f: TxFeatures) -> Tuple[float, List[str]]:
    cfg = settings.detectors.sniper
    w = cfg.weights
    reasons: List[str] = []
    score = 0.0

    if f.token_age_hours <= float(cfg.token_age_hours_max):
        score += float(w.token_age); reasons.append("new_token")

    if f.priority_ratio >= float(cfg.priority_ratio_min):
        score += float(w.priority_ratio); reasons.append("high_priority_ratio")

    if f.slippage_tolerance >= float(cfg.slippage_min):
        score += float(w.slippage); reasons.append("high_slippage")

    if f.is_trending:
        score += float(w.trending); reasons.append("trending")

    if f.path_len == 2:  # simple WETH/USDC -> token path
        score += float(w.path_simple); reasons.append("simple_path")

    low, high = cfg.size_mid_usd
    if low <= f.amount_in_usd <= high:
        score += float(w.size_mid); reasons.append("mid_size_ticket")

    return min(score, 1.0), reasons


def is_sniper(f: TxFeatures) -> Tuple[bool, float, List[str]]:
    score, reasons = score_sniper(f)
    decision = score >= float(settings.detectors.sniper.threshold)
    _log_prediction("sniper", decision)
    return decision, score, reasons


# --- Sandwich victim detector ------------------------------------------------

def score_sandwich_victim(f: TxFeatures) -> Tuple[float, List[str]]:
    cfg = settings.detectors.sandwich_victim
    w = cfg.weights
    reasons: List[str] = []
    score = 0.0

    if not f.is_exact_output:
        score += float(w.exact_input); reasons.append("exact_input")

    if f.slippage_tolerance >= float(cfg.victim_slippage_min):
        score += float(w.slippage); reasons.append("high_slippage")

    if f.size_vs_pool >= float(cfg.size_pool_ratio_min):
        score += float(w.size_vs_pool); reasons.append("large_vs_pool")

    if 0 <= f.deadline_seconds <= int(cfg.deadline_max_s):
        score += float(w.deadline); reasons.append("short_deadline")

    if f.priority_ratio <= float(cfg.priority_ratio_max):
        score += float(w.low_priority); reasons.append("low_priority_ratio")

    if f.path_len == 2:
        score += float(w.path_simple); reasons.append("simple_path")

    return min(score, 1.0), reasons


def is_sandwich_victim(f: TxFeatures) -> Tuple[bool, float, List[str]]:
    score, reasons = score_sandwich_victim(f)
    decision = score >= float(settings.detectors.sandwich_victim.threshold)
    _log_prediction("sandwich_victim", decision)
    return decision, score, reasons


# --- (Optional) Real-time correlation for sandwich fronts --------------------
# If you maintain a short recent index per pair_id, you can flag likely fronts
# when a high-priority tx appears shortly after a vulnerable victim on same pair.
# This is a simple stub you can extend; not required for acceptance today.

# --- Metrics helpers ---------------------------------------------------------

def _log_prediction(detector: str, positive: bool):
    detector_predictions_total.labels(detector=detector, predicted="positive" if positive else "negative").inc()


# --- Evaluation on labeled fixtures -----------------------------------------

def evaluate_on_fixtures(
    detector: str,
    fixtures: List[Tuple[TxFeatures, bool]],
) -> Dict[str, float]:
    """
    Evaluate a single detector on (features, actual_is_positive) fixtures.
    Updates confusion matrix counters and precision/recall/FPR gauges.
    Returns dict with metrics.
    """
    tp = fp = tn = fn = 0
    for f, actual in fixtures:
        if detector == "sniper":
            pred, _, _ = is_sniper(f)
        elif detector == "sandwich_victim":
            pred, _, _ = is_sandwich_victim(f)
        else:
            raise ValueError("unknown detector")

        # confusion
        if actual and pred:   tp += 1; detector_confusion_total.labels(detector, "positive", "positive").inc()
        if actual and not pred: fn += 1; detector_confusion_total.labels(detector, "positive", "negative").inc()
        if (not actual) and pred: fp += 1; detector_confusion_total.labels(detector, "negative", "positive").inc()
        if (not actual) and not pred: tn += 1; detector_confusion_total.labels(detector, "negative", "negative").inc()

    precision = (tp / (tp + fp)) if (tp + fp) else 0.0
    recall    = (tp / (tp + fn)) if (tp + fn) else 0.0
    fpr       = (fp / (fp + tn)) if (fp + tn) else 0.0

    # emit gauges
    detector_precision.labels(detector=detector).set(precision)
    detector_recall.labels(detector=detector).set(recall)
    detector_false_positive_rate.labels(detector=detector).set(fpr)

    return {"precision": precision, "recall": recall, "false_positive_rate": fpr, "tp": tp, "fp": fp, "tn": tn, "fn": fn}

# Added for tests: import gauges used in evaluate_on_fixtures
from bot.core.telemetry import detector_precision, detector_recall, detector_false_positive_rate

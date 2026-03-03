from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from bot.core.canonical import ctx_labels
from bot.core.invariants import get_runtime_invariants
from bot.core.telemetry import mempool_stream_group_lag, risk_state_gauge, trades_failed_total, trades_sent_total

try:
    from ops import metrics as ops_metrics
except Exception:
    ops_metrics = None


def _safe_value(child: Any) -> Optional[float]:
    try:
        return float(child._value.get())
    except Exception:
        return None


def _metric_sum(metric: Any, match: Dict[str, str]) -> float:
    if metric is None:
        return 0.0
    total = 0.0
    labelnames = tuple(getattr(metric, "_labelnames", ()) or ())
    metrics = getattr(metric, "_metrics", {}) or {}
    for labels, child in metrics.items():
        if labelnames:
            lbl = {labelnames[i]: str(labels[i]) for i in range(min(len(labelnames), len(labels)))}
            if any(lbl.get(k) != v for k, v in match.items()):
                continue
        val = _safe_value(child)
        if val is not None:
            total += val
    return float(total)


def _metric_max(metric: Any, match: Dict[str, str]) -> Optional[float]:
    if metric is None:
        return None
    out: Optional[float] = None
    labelnames = tuple(getattr(metric, "_labelnames", ()) or ())
    metrics = getattr(metric, "_metrics", {}) or {}
    for labels, child in metrics.items():
        if labelnames:
            lbl = {labelnames[i]: str(labels[i]) for i in range(min(len(labelnames), len(labels)))}
            if any(lbl.get(k) != v for k, v in match.items()):
                continue
        val = _safe_value(child)
        if val is None:
            continue
        if out is None or val > out:
            out = val
    return out


def _series_by_label(metric: Any, match: Dict[str, str], key_label: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if metric is None:
        return out
    labelnames = tuple(getattr(metric, "_labelnames", ()) or ())
    metrics = getattr(metric, "_metrics", {}) or {}
    for labels, child in metrics.items():
        lbl = {labelnames[i]: str(labels[i]) for i in range(min(len(labelnames), len(labels)))}
        if any(lbl.get(k) != v for k, v in match.items()):
            continue
        key = str(lbl.get(key_label, "")).strip().lower() or "unknown"
        val = _safe_value(child)
        if val is None:
            continue
        out[key] = out.get(key, 0.0) + float(val)
    return out


def _histogram_quantile(metric: Any, match: Dict[str, str], q: float) -> Optional[float]:
    if metric is None:
        return None
    labelnames = tuple(getattr(metric, "_labelnames", ()) or ())
    metrics = getattr(metric, "_metrics", {}) or {}
    bounds: Optional[list[float]] = None
    counts: list[float] = []
    for labels, child in metrics.items():
        lbl = {labelnames[i]: str(labels[i]) for i in range(min(len(labelnames), len(labels)))}
        if any(lbl.get(k) != v for k, v in match.items()):
            continue
        upper_bounds = list(getattr(child, "_upper_bounds", []) or [])
        buckets = list(getattr(child, "_buckets", []) or [])
        if not upper_bounds or not buckets or len(upper_bounds) != len(buckets):
            continue
        if bounds is None:
            bounds = [float(b) for b in upper_bounds]
            counts = [0.0 for _ in bounds]
        for i, bucket in enumerate(buckets):
            try:
                counts[i] += float(bucket.get())  # prometheus_client mutex values
            except Exception:
                try:
                    counts[i] += float(bucket._value.get())  # pragma: no cover - fallback
                except Exception:
                    pass
    if not bounds or not counts:
        return None
    total = counts[-1]
    if total <= 0:
        return None
    target = max(0.0, min(1.0, float(q))) * total
    for b, c in zip(bounds, counts):
        if c >= target:
            if b == float("inf"):
                return bounds[-2] if len(bounds) >= 2 else None
            return float(b)
    return None


def _quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(float(v) for v in values)
    idx = int(round(max(0.0, min(1.0, float(q))) * (len(vals) - 1)))
    return float(vals[idx])


class HealthSnapshotWriter:
    def __init__(
        self,
        *,
        path: str = "ops/health_snapshot.json",
        interval_s: float = 10.0,
        window_s: float = 600.0,
    ) -> None:
        self.path = Path(path)
        self.interval_s = max(1.0, float(interval_s))
        self.window_s = max(60.0, float(window_s))
        self._last_write_ts = 0.0
        self._trade_samples: Deque[tuple[float, float, float]] = deque(maxlen=4096)
        self._opportunity_samples: Deque[tuple[float, float, float, float, float]] = deque(maxlen=4096)
        self._rpc_error_samples: Deque[tuple[float, float]] = deque(maxlen=4096)
        self._dex_quote_fail_samples: Deque[tuple[float, Dict[str, float]]] = deque(maxlen=4096)
        self._reject_samples: Deque[tuple[float, Dict[str, float]]] = deque(maxlen=4096)
        self._last_sent_total = 0.0
        self._last_trade_ts: Optional[int] = None

    def maybe_write(
        self,
        *,
        family: str,
        chain: str,
        state: str,
        mode: str,
        force: bool = False,
        now: Optional[float] = None,
    ) -> bool:
        t = time.time() if now is None else float(now)
        if not force and (t - self._last_write_ts) < self.interval_s:
            return False
        payload = self._build_payload(family=family, chain=chain, state=state, mode=mode, now=t)
        self._write_atomic(payload)
        self._last_write_ts = t
        return True

    def _build_payload(self, *, family: str, chain: str, state: str, mode: str, now: float) -> Dict[str, Any]:
        ctx = ctx_labels(family=family, chain=chain, strategy="default")
        fam, ch, network = ctx["family"], ctx["chain"], ctx["network"]
        match_chain = {"chain_family": fam, "chain": ch}
        filter_ops = {"family": fam, "chain": ch, "network": network}
        sent_total = 0.0
        failed_total = 0.0
        opp_seen_total = 0.0
        opp_attempted_total = 0.0
        opp_filled_total = 0.0
        opp_executed_total = 0.0
        rpc_errors_total = 0.0

        if ops_metrics is not None:
            sent_total = _metric_sum(getattr(ops_metrics, "tx_sent_total", None), filter_ops)
            failed_total = _metric_sum(getattr(ops_metrics, "tx_failed_total", None), filter_ops)
            opp_seen_total = _metric_sum(getattr(ops_metrics, "opportunities_seen_total", None), filter_ops)
            opp_attempted_total = _metric_sum(getattr(ops_metrics, "opportunities_attempted_total", None), filter_ops)
            opp_filled_total = _metric_sum(getattr(ops_metrics, "opportunities_filled_total", None), filter_ops)
            opp_executed_total = _metric_sum(getattr(ops_metrics, "opportunities_executed_total", None), filter_ops)
            rpc_errors_total = _metric_sum(getattr(ops_metrics, "rpc_errors_total", None), filter_ops)
        if sent_total <= 0 and failed_total <= 0:
            # Backward-compatible fallback to legacy counters if standardized metrics are absent.
            sent_total = _metric_sum(trades_sent_total, match_chain)
            failed_total = _metric_sum(trades_failed_total, match_chain)

        self._trade_samples.append((now, float(sent_total), float(failed_total)))
        self._opportunity_samples.append(
            (
                now,
                float(opp_seen_total),
                float(opp_attempted_total),
                float(opp_filled_total),
                float(opp_executed_total),
            )
        )
        self._rpc_error_samples.append((now, float(rpc_errors_total)))
        cutoff = now - self.window_s
        while self._trade_samples and self._trade_samples[0][0] < cutoff:
            self._trade_samples.popleft()
        while self._opportunity_samples and self._opportunity_samples[0][0] < cutoff:
            self._opportunity_samples.popleft()
        while self._rpc_error_samples and self._rpc_error_samples[0][0] < cutoff:
            self._rpc_error_samples.popleft()
        while self._dex_quote_fail_samples and self._dex_quote_fail_samples[0][0] < cutoff:
            self._dex_quote_fail_samples.popleft()
        while self._reject_samples and self._reject_samples[0][0] < cutoff:
            self._reject_samples.popleft()

        if sent_total > self._last_sent_total:
            self._last_trade_ts = int(now)
        self._last_sent_total = sent_total

        sent_10m = 0.0
        failed_10m = 0.0
        if len(self._trade_samples) >= 2:
            _, s0, f0 = self._trade_samples[0]
            _, s1, f1 = self._trade_samples[-1]
            sent_10m = max(0.0, s1 - s0)
            failed_10m = max(0.0, f1 - f0)
        opp_seen_10m = 0.0
        opp_attempted_10m = 0.0
        opp_filled_10m = 0.0
        opp_executed_10m = 0.0
        if len(self._opportunity_samples) >= 2:
            _, a0, b0, c0, d0 = self._opportunity_samples[0]
            _, a1, b1, c1, d1 = self._opportunity_samples[-1]
            opp_seen_10m = max(0.0, a1 - a0)
            opp_attempted_10m = max(0.0, b1 - b0)
            opp_filled_10m = max(0.0, c1 - c0)
            opp_executed_10m = max(0.0, d1 - d0)
        rpc_errors_10m = 0.0
        if len(self._rpc_error_samples) >= 2:
            _, e0 = self._rpc_error_samples[0]
            _, e1 = self._rpc_error_samples[-1]
            rpc_errors_10m = max(0.0, e1 - e0)

        inv = get_runtime_invariants()
        snap = inv.snapshot(now=now)
        rpc_p99_ms = snap.get("rpc_p99_ms")
        rpc_vals = [v for ts, v in getattr(inv, "_rpc_samples", []) if ts >= cutoff]
        rpc_p95_ms = _quantile(rpc_vals, 0.95)
        confirm_p95_ms: Optional[float] = None
        if ops_metrics is not None:
            q = _histogram_quantile(getattr(ops_metrics, "tx_confirm_latency_seconds", None), filter_ops, 0.95)
            if q is not None:
                confirm_p95_ms = float(q) * 1000.0

        head = None
        slot = None
        head_lag = None
        slot_lag = None
        pnl_today = None
        drawdown = None
        fees_today = None
        if ops_metrics is not None:
            head = _metric_max(getattr(ops_metrics, "chain_head", None), filter_ops)
            slot = _metric_max(getattr(ops_metrics, "chain_slot", None), filter_ops)
            head_lag = _metric_max(getattr(ops_metrics, "head_lag_blocks", None), filter_ops)
            slot_lag = _metric_max(getattr(ops_metrics, "slot_lag", None), filter_ops)
            pnl_today = _metric_sum(getattr(ops_metrics, "pnl_realized_usd", None), filter_ops)
            drawdown = _metric_max(getattr(ops_metrics, "drawdown_usd", None), filter_ops)
            fees_today = _metric_sum(getattr(ops_metrics, "fees_total_usd", None), filter_ops)

        dex_health_summary: Dict[str, Dict[str, Optional[float]]] = {}
        reject_reasons_top: Dict[str, int] = {}
        top_opportunities_count: int = 0
        if ops_metrics is not None:
            quote_fail_metric = (
                getattr(ops_metrics, "quote_fail_total", None)
                or getattr(ops_metrics, "dex_quote_fail_total", None)
                or getattr(ops_metrics, "quoter_fail_total", None)
            )
            quote_latency_metric = (
                getattr(ops_metrics, "quote_latency_seconds", None)
                or getattr(ops_metrics, "dex_quote_latency_seconds", None)
                or getattr(ops_metrics, "quoter_latency_seconds", None)
            )
            current_fail = _series_by_label(quote_fail_metric, filter_ops, "dex")
            if current_fail:
                self._dex_quote_fail_samples.append((now, current_fail))
                if len(self._dex_quote_fail_samples) >= 2:
                    base_map = self._dex_quote_fail_samples[0][1]
                    cur_map = self._dex_quote_fail_samples[-1][1]
                    for dex in sorted(set(base_map.keys()) | set(cur_map.keys())):
                        qf = max(0.0, float(cur_map.get(dex, 0.0) - base_map.get(dex, 0.0)))
                        dex_health_summary[dex] = {"quote_fail_10m": qf, "quote_p95_ms": None}
            if quote_latency_metric is not None:
                # p95 per dex if histogram metric exists.
                labelnames = tuple(getattr(quote_latency_metric, "_labelnames", ()) or ())
                has_dex = "dex" in labelnames
                if has_dex:
                    for dex in list(dex_health_summary.keys()) or ["unknown"]:
                        q = _histogram_quantile(quote_latency_metric, dict(filter_ops, dex=dex), 0.95)
                        if q is not None:
                            dex_health_summary.setdefault(dex, {"quote_fail_10m": 0.0, "quote_p95_ms": None})
                            dex_health_summary[dex]["quote_p95_ms"] = float(q) * 1000.0

            q_depth = _metric_max(getattr(ops_metrics, "opportunity_queue_depth", None), filter_ops)
            if q_depth is not None:
                top_opportunities_count = int(max(0.0, q_depth))

            rej_metric = getattr(ops_metrics, "opportunities_rejected_total", None)
            current_rejects = _series_by_label(rej_metric, filter_ops, "reason")
            if current_rejects:
                self._reject_samples.append((now, current_rejects))
                if len(self._reject_samples) >= 2:
                    base_map = self._reject_samples[0][1]
                    cur_map = self._reject_samples[-1][1]
                    ranked: list[tuple[str, int]] = []
                    for reason in sorted(set(base_map.keys()) | set(cur_map.keys())):
                        delta = max(0.0, float(cur_map.get(reason, 0.0) - base_map.get(reason, 0.0)))
                        if delta > 0:
                            ranked.append((reason, int(delta)))
                    ranked.sort(key=lambda x: x[1], reverse=True)
                    reject_reasons_top = {k: v for k, v in ranked[:5]}

        if pnl_today is None:
            pnl_today = _metric_max(risk_state_gauge, {"key": "daily_pnl_usd"})

        lag = head_lag if head_lag is not None else slot_lag
        if lag is None:
            lag = _metric_max(mempool_stream_group_lag, {})

        last_trade_time = None
        if self._last_trade_ts is not None:
            last_trade_time = datetime.fromtimestamp(self._last_trade_ts, tz=timezone.utc).isoformat()

        return {
            "ts": int(now),
            "family": fam,
            "chain_family": fam,
            "chain": ch,
            "network": network,
            "state": state,
            "mode": mode,
            "head": head,
            "slot": slot,
            "lag": lag,
            "head_lag": head_lag,
            "slot_lag": slot_lag,
            "last_trade_ts": self._last_trade_ts,
            "last_trade_time": last_trade_time,
            "tx_sent_10m": int(sent_10m),
            "tx_failed_10m": int(failed_10m),
            "trades_sent_10m": int(sent_10m),
            "trades_failed_10m": int(failed_10m),
            "rpc_p95_ms": rpc_p95_ms,
            "rpc_p99_ms": rpc_p99_ms,
            "rpc_errors_10m": int(rpc_errors_10m),
            "opportunities_seen_10m": int(opp_seen_10m),
            "opportunities_attempted_10m": int(opp_attempted_10m),
            "opportunities_filled_10m": int(opp_filled_10m),
            "opportunities_executed_10m": int(opp_executed_10m),
            "funnel_10m": {
                "seen": int(opp_seen_10m),
                "attempted": int(opp_attempted_10m),
                "filled": int(opp_filled_10m),
                "executed": int(opp_executed_10m),
            },
            "top_opportunities_count": int(top_opportunities_count),
            "top_reject_reasons_10m": reject_reasons_top,
            "confirm_p95_ms": confirm_p95_ms,
            "pnl_today_usd": pnl_today,
            "drawdown_usd": drawdown,
            "fees_today_usd": fees_today,
            "errors_last_10m": int(snap.get("errors_last_10m") or 0),
            "tx_failure_rate": snap.get("tx_failure_rate"),
            "dex_health_summary": dex_health_summary,
        }

    def _write_atomic(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

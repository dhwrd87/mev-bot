from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _sum_metric(metrics_text: str, metric_name: str) -> float:
    total = 0.0
    for raw in metrics_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(metric_name):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    total += float(parts[-1])
                except Exception:
                    pass
    return total


def _histogram_p95(metrics_text: str, base_name: str) -> Optional[float]:
    buckets: list[tuple[float, float]] = []
    for raw in metrics_text.splitlines():
        line = raw.strip()
        if not line.startswith(f"{base_name}_bucket"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            value = float(parts[-1])
        except Exception:
            continue
        le_idx = line.find('le="')
        if le_idx < 0:
            continue
        start = le_idx + 4
        end = line.find('"', start)
        le_raw = line[start:end]
        if le_raw == "+Inf":
            continue
        try:
            le = float(le_raw)
        except Exception:
            continue
        buckets.append((le, value))
    if not buckets:
        return None
    buckets.sort(key=lambda x: x[0])
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = total * 0.95
    for le, count in buckets:
        if count >= target:
            return le
    return buckets[-1][0]


def _format_ts_utc_from_seconds(v: float) -> str:
    if v <= 0:
        return "—"
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()


def _fmt_num(v: Any, suffix: str = "", digits: int = 2) -> str:
    try:
        f = float(v)
    except Exception:
        return "—"
    if f != f:  # NaN
        return "—"
    return f"{f:.{digits}f}{suffix}"


def parse_metrics_text(metrics_text: str) -> Dict[str, Any]:
    sent = _sum_metric(metrics_text, "trades_sent_total")
    failed = _sum_metric(metrics_text, "trades_failed_total")
    p95 = _histogram_p95(metrics_text, "rpc_latency_seconds")

    last_trade_s = None
    for name in (
        "last_trade_timestamp_seconds",
        "mevbot_last_trade_timestamp_seconds",
        "last_trade_ts_seconds",
        "mevbot_last_trade_ts_seconds",
    ):
        val = _sum_metric(metrics_text, name)
        if val > 0:
            last_trade_s = val
            break

    return {
        "trades_sent_total": sent,
        "trades_failed_total": failed,
        "rpc_p95": "—" if p95 is None else f"{p95:.3f}s",
        "last_trade": "—" if last_trade_s is None else _format_ts_utc_from_seconds(last_trade_s),
    }


class StatusDataProvider:
    def __init__(
        self,
        *,
        metrics_scrape_url: str = "",
        snapshot_path: str = "ops/health_snapshot.json",
        snapshot_stale_after_s: float = 60.0,
    ) -> None:
        self.metrics_scrape_url = str(metrics_scrape_url or "").strip()
        self.snapshot_path = str(snapshot_path or "").strip()
        self.snapshot_stale_after_s = max(5.0, float(snapshot_stale_after_s))
        self._counter_window: deque[tuple[float, float, float]] = deque(maxlen=1200)
        self._last_metrics: Dict[str, Any] = {}

    def _push_counters(self, sent: float, failed: float) -> None:
        now = time.time()
        self._counter_window.append((now, sent, failed))
        cutoff = now - 600.0
        while self._counter_window and self._counter_window[0][0] < cutoff:
            self._counter_window.popleft()

    def _trades_10m(self) -> str:
        if len(self._counter_window) < 2:
            return "—"
        _, s0, f0 = self._counter_window[0]
        _, s1, f1 = self._counter_window[-1]
        return f"{int(max(0.0, s1 - s0))}/{int(max(0.0, f1 - f0))}"

    def _load_snapshot(self) -> Dict[str, Any]:
        if not self.snapshot_path:
            return {}
        p = Path(self.snapshot_path)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        chain_family = str(raw.get("chain_family", "")).strip()
        chain = str(raw.get("chain", "")).strip()
        network = str(raw.get("network", "")).strip()
        active_chain = str(raw.get("active_chain", "")).strip()
        if not active_chain and chain_family and chain:
            active_chain = f"{chain_family}:{chain}"
            if network:
                active_chain = f"{active_chain} ({network})"
        head = raw.get("head")
        slot = raw.get("slot")
        head_lag = raw.get("head_lag")
        slot_lag = raw.get("slot_lag")
        lag = raw.get("lag")
        if lag is None:
            lag = head_lag if head_lag is not None else slot_lag
        errors_10m = raw.get("errors_last_10m")
        ts = raw.get("ts")
        tx_sent_10m = raw.get("tx_sent_10m", raw.get("trades_sent_10m"))
        tx_failed_10m = raw.get("tx_failed_10m", raw.get("trades_failed_10m"))
        rpc_p95_ms = raw.get("rpc_p95_ms")
        rpc_p99_ms = raw.get("rpc_p99_ms")
        pnl_today = raw.get("pnl_today_usd")
        drawdown = raw.get("drawdown_usd")
        fees_today = raw.get("fees_today_usd")
        out: Dict[str, Any] = {}
        if active_chain:
            out["active_chain"] = active_chain
        if ts:
            try:
                ts_f = float(ts)
                out["heartbeat_utc"] = _format_ts_utc_from_seconds(ts_f)
                age_s = max(0.0, time.time() - ts_f)
                out["snapshot_age_s"] = age_s
                out["stale"] = age_s > self.snapshot_stale_after_s
            except Exception:
                pass
        if raw.get("last_trade_time"):
            out["last_trade"] = str(raw.get("last_trade_time"))
        elif raw.get("last_trade_ts"):
            try:
                out["last_trade"] = _format_ts_utc_from_seconds(float(raw.get("last_trade_ts")))
            except Exception:
                pass

        if tx_sent_10m is not None and tx_failed_10m is not None:
            out["trades_10m"] = f"{int(float(tx_sent_10m))}/{int(float(tx_failed_10m))}"
        out["tx_sent_10m"] = tx_sent_10m if tx_sent_10m is not None else "—"
        out["tx_failed_10m"] = tx_failed_10m if tx_failed_10m is not None else "—"
        out["rpc_errors_10m"] = raw.get("rpc_errors_10m", "—")
        out["opportunities_seen_10m"] = raw.get("opportunities_seen_10m", "—")
        out["opportunities_attempted_10m"] = raw.get("opportunities_attempted_10m", "—")
        out["opportunities_filled_10m"] = raw.get("opportunities_filled_10m", "—")
        out["opportunities_executed_10m"] = raw.get("opportunities_executed_10m", "—")
        out["confirm_p95_ms"] = raw.get("confirm_p95_ms", "—")
        out["dex_health_summary"] = raw.get("dex_health_summary") if isinstance(raw.get("dex_health_summary"), dict) else {}

        if rpc_p95_ms is not None:
            out["rpc_p95"] = _fmt_num(rpc_p95_ms, "ms", digits=1)
        if rpc_p99_ms is not None:
            out["rpc_p99"] = _fmt_num(rpc_p99_ms, "ms", digits=1)

        h = _fmt_num(head, "", digits=0)
        s = _fmt_num(slot, "", digits=0)
        l = _fmt_num(lag, "", digits=2)
        if h != "—" or s != "—" or l != "—":
            out["head_slot_lag"] = f"head={h} slot={s} lag={l}"

        pnl = _fmt_num(pnl_today, " USD", digits=2)
        dd = _fmt_num(drawdown, " USD", digits=2)
        fees = _fmt_num(fees_today, " USD", digits=2)
        if pnl != "—" or dd != "—" or fees != "—":
            out["pnl_drawdown_fees"] = f"pnl={pnl} drawdown={dd} fees={fees}"

        rpc_err = raw.get("rpc_errors_10m")
        if rpc_err is not None and errors_10m is not None:
            out["error_counters"] = f"rpc_errors_10m={rpc_err} tx_errors_10m={errors_10m}"
            if lag is not None:
                out["error_counters"] = f"{out['error_counters']} lag={lag}"
        elif errors_10m is not None and lag is not None:
            out["error_counters"] = f"errors_10m={errors_10m} lag={lag}"
        elif errors_10m is not None:
            out["error_counters"] = f"errors_10m={errors_10m}"
        elif lag is not None:
            out["error_counters"] = f"lag={lag}"
        return out

    async def collect(self, http_client) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "active_chain": "—",
            "heartbeat_utc": "—",
            "head_slot_lag": "—",
            "rpc_p95": "—",
            "rpc_p99": "—",
            "trades_10m": "—",
            "last_trade": "—",
            "pnl_drawdown_fees": "—",
            "error_counters": "—",
            "stale": False,
            "snapshot_age_s": None,
            "snapshot_status": "OK",
            "opportunities_10m": "—",
            "confirm_p95": "—",
            "dex_health": "—",
        }
        out.update(self._load_snapshot())
        if out.get("stale"):
            age_s = out.get("snapshot_age_s")
            age_txt = f"{int(float(age_s))}s" if isinstance(age_s, (int, float)) else "unknown"
            out["snapshot_status"] = f"STALE ({age_txt})"
            hb = str(out.get("heartbeat_utc", "—"))
            out["heartbeat_utc"] = f"{hb} [STALE]"
        else:
            out["snapshot_status"] = "OK"

        seen = out.get("opportunities_seen_10m")
        attempted = out.get("opportunities_attempted_10m")
        executed = out.get("opportunities_executed_10m")
        filled = out.get("opportunities_filled_10m")
        terminal = executed if executed not in (None, "—") else filled
        if seen != "—" or attempted != "—" or terminal != "—":
            out["opportunities_10m"] = f"{seen}/{attempted}/{terminal}"
        cp95 = out.get("confirm_p95_ms")
        if cp95 not in (None, "—"):
            out["confirm_p95"] = _fmt_num(cp95, "ms", digits=1)
        dex_health = out.get("dex_health_summary")
        if isinstance(dex_health, dict) and dex_health:
            parts = []
            for dex in sorted(dex_health.keys())[:3]:
                row = dex_health.get(dex) or {}
                qf = row.get("quote_fail_10m", "—")
                qp = row.get("quote_p95_ms", "—")
                parts.append(f"{dex}:fail={qf},p95={_fmt_num(qp, 'ms', 1)}")
            out["dex_health"] = "; ".join(parts)

        if not self.metrics_scrape_url:
            return out

        try:
            resp = await http_client.get(self.metrics_scrape_url)
            resp.raise_for_status()
            parsed = parse_metrics_text(resp.text)
            sent = float(parsed.get("trades_sent_total", 0.0))
            failed = float(parsed.get("trades_failed_total", 0.0))
            self._push_counters(sent, failed)
            metrics_view = {
                "rpc_p95": parsed.get("rpc_p95", "—"),
                "trades_10m": self._trades_10m(),
                "last_trade": parsed.get("last_trade", "—"),
                "error_counters": f"failed_trades_total={int(failed)}",
                "stale": False,
            }
            self._last_metrics = dict(metrics_view)
            out.update(metrics_view)
            return out
        except Exception:
            if self._last_metrics:
                stale = dict(self._last_metrics)
                stale["stale"] = True
                if stale.get("error_counters", "—") != "—":
                    stale["error_counters"] = f"{stale['error_counters']} (stale)"
                out.update(stale)
            else:
                out["stale"] = True
                out["error_counters"] = "metrics_unavailable (stale)"
            return out

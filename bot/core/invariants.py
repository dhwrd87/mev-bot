from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from bot.core.state import BotState


@dataclass
class RuntimeInvariants:
    rpc_p99_ms_threshold: float
    rpc_window_min: int
    drawdown_threshold: float
    tx_fail_rate_threshold: float
    tx_fail_window_min: int
    _rpc_samples: Deque[Tuple[float, float]]
    _tx_samples: Deque[Tuple[float, bool]]

    @classmethod
    def from_env(cls) -> "RuntimeInvariants":
        return cls(
            rpc_p99_ms_threshold=float(os.getenv("INVAR_RPC_P99_MS_THRESHOLD", "1500")),
            rpc_window_min=int(os.getenv("INVAR_RPC_P99_WINDOW_MIN", "5")),
            drawdown_threshold=float(os.getenv("INVAR_DRAWDOWN_THRESHOLD", "0.20")),
            tx_fail_rate_threshold=float(os.getenv("INVAR_TX_FAIL_RATE_THRESHOLD", "0.50")),
            tx_fail_window_min=int(os.getenv("INVAR_TX_FAIL_RATE_WINDOW_MIN", "5")),
            _rpc_samples=deque(maxlen=20000),
            _tx_samples=deque(maxlen=20000),
        )

    def observe_rpc_latency_ms(self, latency_ms: float, *, now: Optional[float] = None) -> None:
        t = time.time() if now is None else float(now)
        self._rpc_samples.append((t, max(0.0, float(latency_ms))))
        self._trim(now=t)

    def observe_tx_result(self, ok: bool, *, now: Optional[float] = None) -> None:
        t = time.time() if now is None else float(now)
        self._tx_samples.append((t, bool(ok)))
        self._trim(now=t)

    def evaluate(
        self,
        *,
        operator_state: Optional[Dict[str, object]] = None,
        drawdown: Optional[float] = None,
        now: Optional[float] = None,
    ) -> tuple[BotState, str]:
        t = time.time() if now is None else float(now)
        self._trim(now=t)

        op = operator_state or {}
        if bool(op.get("kill_switch", False)):
            return BotState.PANIC, "operator_kill_switch"
        if str(op.get("state", "TRADING")).upper() != BotState.TRADING.value:
            return BotState.PAUSED, "operator_not_trading"

        dd = drawdown
        if dd is None:
            try:
                dd = float(os.getenv("BOT_DRAWDOWN", "0"))
            except Exception:
                dd = 0.0
        if float(dd) > self.drawdown_threshold:
            return BotState.PANIC, "drawdown_limit"

        rpc_p99 = self.rpc_p99_ms(now=t)
        if rpc_p99 is not None and rpc_p99 > self.rpc_p99_ms_threshold:
            return BotState.DEGRADED, "rpc_p99_high"

        fail_rate = self.tx_failure_rate(now=t)
        if fail_rate is not None and fail_rate > self.tx_fail_rate_threshold:
            return BotState.DEGRADED, "tx_failure_rate_high"

        return BotState.READY, "healthy"

    def rpc_p99_ms(self, *, now: Optional[float] = None) -> Optional[float]:
        t = time.time() if now is None else float(now)
        cutoff = t - float(self.rpc_window_min * 60)
        vals = [v for ts, v in self._rpc_samples if ts >= cutoff]
        if not vals:
            return None
        vals.sort()
        idx = max(0, min(len(vals) - 1, int(round(0.99 * (len(vals) - 1)))))
        return float(vals[idx])

    def tx_failure_rate(self, *, now: Optional[float] = None) -> Optional[float]:
        t = time.time() if now is None else float(now)
        cutoff = t - float(self.tx_fail_window_min * 60)
        vals = [ok for ts, ok in self._tx_samples if ts >= cutoff]
        if not vals:
            return None
        fails = sum(1 for ok in vals if not ok)
        return float(fails) / float(len(vals))

    def errors_last_10m(self, *, now: Optional[float] = None) -> int:
        t = time.time() if now is None else float(now)
        cutoff = t - 600.0
        return sum(1 for ts, ok in self._tx_samples if ts >= cutoff and not ok)

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, object]:
        t = time.time() if now is None else float(now)
        return {
            "rpc_p99_ms": self.rpc_p99_ms(now=t),
            "tx_failure_rate": self.tx_failure_rate(now=t),
            "errors_last_10m": self.errors_last_10m(now=t),
            "ts": int(t),
        }

    def _trim(self, *, now: float) -> None:
        max_window = max(self.rpc_window_min, self.tx_fail_window_min, 10) * 60
        cutoff = now - float(max_window)
        while self._rpc_samples and self._rpc_samples[0][0] < cutoff:
            self._rpc_samples.popleft()
        while self._tx_samples and self._tx_samples[0][0] < cutoff:
            self._tx_samples.popleft()


_RUNTIME: RuntimeInvariants | None = None


def get_runtime_invariants() -> RuntimeInvariants:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = RuntimeInvariants.from_env()
    return _RUNTIME

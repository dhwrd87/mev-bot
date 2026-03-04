from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from ops.metrics import (
    map_revert_reason,
    record_rpc_error,
    record_rpc_latency,
    record_tx_confirm_latency,
    record_tx_confirmed,
    record_tx_failed,
    record_tx_revert,
    record_tx_sent,
)

try:
    from bot.core.telemetry import rpc_circuit_breaker_open, rpc_circuit_breaker_trips_total
except Exception:  # pragma: no cover
    rpc_circuit_breaker_open = None
    rpc_circuit_breaker_trips_total = None


def _now() -> float:
    return time.monotonic()


def _family_for_chain(chain: str) -> str:
    c = str(chain or "").lower()
    return "sol" if c.startswith("sol") else "evm"


@dataclass(frozen=True)
class FeePolicy:
    max_fee_gwei: float
    escalation_factor: float = 1.125
    max_escalations: int = 4
    profit_safety_margin_usd: float = 0.0


@dataclass(frozen=True)
class FeeDecision:
    allowed: bool
    fee_gwei: float
    reason: str
    escalation_step: int = 0


def estimate_fee_usd(*, gas_limit: int, gas_price_gwei: float, native_usd: float) -> float:
    return max(0.0, float(gas_limit)) * max(0.0, float(gas_price_gwei)) * 1e-9 * max(0.0, float(native_usd))


def choose_fee_gwei(
    *,
    policy: FeePolicy,
    initial_fee_gwei: float,
    gas_limit: int,
    expected_profit_usd: float,
    native_usd: float,
) -> FeeDecision:
    fee = max(0.0, float(initial_fee_gwei))
    for step in range(max(0, int(policy.max_escalations)) + 1):
        if fee > float(policy.max_fee_gwei):
            return FeeDecision(False, float(policy.max_fee_gwei), "fee_cap_exceeded", step)
        total_cost = estimate_fee_usd(gas_limit=gas_limit, gas_price_gwei=fee, native_usd=native_usd)
        if expected_profit_usd >= total_cost + float(policy.profit_safety_margin_usd):
            return FeeDecision(True, fee, "ok", step)
        fee *= max(1.0, float(policy.escalation_factor))
    return FeeDecision(False, min(fee, float(policy.max_fee_gwei)), "not_profitable_after_escalation", policy.max_escalations)


class RpcCircuitBreaker:
    def __init__(self, *, window_size: int = 50, error_ratio_threshold: float = 0.5, open_seconds: float = 10.0) -> None:
        self.window_size = max(5, int(window_size))
        self.error_ratio_threshold = max(0.0, min(1.0, float(error_ratio_threshold)))
        self.open_seconds = max(1.0, float(open_seconds))
        self._events: Deque[int] = deque(maxlen=self.window_size)  # 1 if fail, 0 if success
        self._open_until: float = 0.0

    def is_open(self) -> bool:
        return _now() < self._open_until

    def record(self, ok: bool) -> None:
        self._events.append(0 if ok else 1)
        if not ok and self.error_ratio() >= self.error_ratio_threshold and len(self._events) >= min(10, self.window_size):
            self._open_until = _now() + self.open_seconds
            if rpc_circuit_breaker_trips_total is not None:
                rpc_circuit_breaker_trips_total.inc()
        if rpc_circuit_breaker_open is not None:
            rpc_circuit_breaker_open.set(1 if self.is_open() else 0)

    def error_ratio(self) -> float:
        if not self._events:
            return 0.0
        return float(sum(self._events)) / float(len(self._events))


class RpcCaller:
    def __init__(
        self,
        *,
        chain: str,
        provider: str,
        timeout_s: float = 3.0,
        retries: int = 2,
        backoff_base_s: float = 0.2,
        backoff_max_s: float = 3.0,
        backoff_jitter: float = 0.2,
        circuit: Optional[RpcCircuitBreaker] = None,
    ) -> None:
        self.chain = chain
        self.provider = provider
        self.timeout_s = max(0.1, float(timeout_s))
        self.retries = max(0, int(retries))
        self.backoff_base_s = max(0.01, float(backoff_base_s))
        self.backoff_max_s = max(self.backoff_base_s, float(backoff_max_s))
        self.backoff_jitter = max(0.0, float(backoff_jitter))
        self.circuit = circuit or RpcCircuitBreaker()

    def _sleep_for_attempt(self, attempt: int) -> float:
        base = min(self.backoff_max_s, self.backoff_base_s * (2 ** max(0, attempt)))
        j = base * self.backoff_jitter
        return max(0.01, base + random.uniform(-j, j))

    async def call(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        method: str = "unknown",
        **kwargs: Any,
    ) -> Any:
        family = _family_for_chain(self.chain)
        if self.circuit.is_open():
            record_rpc_error(provider=self.provider, code_bucket="circuit_open", family=family, chain=self.chain)
            raise RuntimeError("rpc_circuit_open")

        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=self.timeout_s)
                self.circuit.record(True)
                record_rpc_latency(
                    family=family,
                    chain=self.chain,
                    provider=self.provider,
                    method=method,
                    seconds=max(0.0, time.perf_counter() - t0),
                )
                return result
            except asyncio.TimeoutError as e:
                last_err = e
                self.circuit.record(False)
                record_rpc_error(provider=self.provider, code_bucket="timeout", family=family, chain=self.chain)
            except Exception as e:
                last_err = e
                self.circuit.record(False)
                record_rpc_error(provider=self.provider, code_bucket="rpc_error", family=family, chain=self.chain)

            record_rpc_latency(
                family=family,
                chain=self.chain,
                provider=self.provider,
                method=method,
                seconds=max(0.0, time.perf_counter() - t0),
            )
            if attempt < self.retries and not self.circuit.is_open():
                await asyncio.sleep(self._sleep_for_attempt(attempt))
                continue
            break
        raise RuntimeError(f"rpc_call_failed:{last_err}")


class EvmNonceManager:
    def __init__(self, *, ttl_s: float = 1.0) -> None:
        self.ttl_s = max(0.0, float(ttl_s))
        self._nonce: Dict[str, int] = {}
        self._ts: Dict[str, float] = {}

    async def next_nonce(self, *, address: str, fetch_nonce: Callable[[str], Awaitable[int]]) -> int:
        a = str(address).lower()
        now = _now()
        cached = self._nonce.get(a)
        if cached is not None and (now - self._ts.get(a, 0.0)) <= self.ttl_s:
            n = int(cached)
            self._nonce[a] = n + 1
            self._ts[a] = now
            return n
        n = int(await fetch_nonce(a))
        self._nonce[a] = n + 1
        self._ts[a] = now
        return n

    def observe_nonce_error(self, *, address: str) -> None:
        self._ts[str(address).lower()] = 0.0


class SolBlockhashManager:
    def __init__(self, *, ttl_s: float = 20.0) -> None:
        self.ttl_s = max(1.0, float(ttl_s))
        self._value: Optional[str] = None
        self._ts: float = 0.0

    async def get_recent_blockhash(self, fetch_blockhash: Callable[[], Awaitable[str]]) -> str:
        now = _now()
        if self._value and (now - self._ts) <= self.ttl_s:
            return self._value
        self._value = str(await fetch_blockhash())
        self._ts = now
        return self._value

    def invalidate(self) -> None:
        self._ts = 0.0


@dataclass(frozen=True)
class ReconcileInput:
    chain: str
    strategy: str
    tx_hash: str
    sent_ts: float
    confirmed_ts: Optional[float]
    gross_pnl_usd: float
    fees_usd: float
    expected_out: Optional[float] = None
    actual_out: Optional[float] = None
    revert_reason: Optional[str] = None
    success: bool = True


@dataclass(frozen=True)
class ReconcileResult:
    tx_hash: str
    success: bool
    realized_pnl_usd: float
    fees_usd: float
    slippage_bps: Optional[float]
    revert_bucket: Optional[str]
    confirm_latency_s: Optional[float]


def reconcile_trade(inp: ReconcileInput) -> ReconcileResult:
    family = _family_for_chain(inp.chain)
    confirm_latency = None
    if inp.confirmed_ts is not None and inp.sent_ts > 0:
        confirm_latency = max(0.0, float(inp.confirmed_ts) - float(inp.sent_ts))
        record_tx_confirm_latency(family=family, chain=inp.chain, seconds=confirm_latency, strategy=inp.strategy)

    realized = float(inp.gross_pnl_usd) - float(inp.fees_usd)
    slippage = None
    if inp.expected_out and inp.actual_out and float(inp.expected_out) > 0:
        slippage = max(0.0, (float(inp.expected_out) - float(inp.actual_out)) / float(inp.expected_out) * 10_000.0)

    revert_bucket = None
    if inp.success:
        record_tx_confirmed(family=family, chain=inp.chain, strategy=inp.strategy)
    else:
        revert_bucket = map_revert_reason(str(inp.revert_reason or "other"))
        record_tx_failed(family=family, chain=inp.chain, strategy=inp.strategy, reason=revert_bucket)
        record_tx_revert(family=family, chain=inp.chain, reason=revert_bucket)

    return ReconcileResult(
        tx_hash=inp.tx_hash,
        success=bool(inp.success),
        realized_pnl_usd=realized,
        fees_usd=float(inp.fees_usd),
        slippage_bps=slippage,
        revert_bucket=revert_bucket,
        confirm_latency_s=confirm_latency,
    )


class ExecutionEngine:
    """
    Minimal reliability-focused execution helper.
    Existing send paths can use this incrementally for policy checks and reconciliation.
    """

    def __init__(self, *, chain_fee_policies: Optional[Dict[str, FeePolicy]] = None) -> None:
        self.chain_fee_policies: Dict[str, FeePolicy] = dict(chain_fee_policies or {})
        self.nonces = EvmNonceManager(ttl_s=float(os.getenv("EVM_NONCE_CACHE_TTL_S", "1")))
        self.blockhash = SolBlockhashManager(ttl_s=float(os.getenv("SOL_BLOCKHASH_TTL_S", "20")))

    def fee_policy_for(self, chain: str) -> FeePolicy:
        c = str(chain).lower()
        if c in self.chain_fee_policies:
            return self.chain_fee_policies[c]
        default_cap = float(os.getenv("EXEC_MAX_FEE_GWEI", "120"))
        return FeePolicy(max_fee_gwei=default_cap)

    def decide_fee(
        self,
        *,
        chain: str,
        initial_fee_gwei: float,
        gas_limit: int,
        expected_profit_usd: float,
        native_usd: float,
    ) -> FeeDecision:
        policy = self.fee_policy_for(chain)
        return choose_fee_gwei(
            policy=policy,
            initial_fee_gwei=initial_fee_gwei,
            gas_limit=gas_limit,
            expected_profit_usd=expected_profit_usd,
            native_usd=native_usd,
        )

    async def reserve_nonce(self, *, address: str, fetch_nonce: Callable[[str], Awaitable[int]]) -> int:
        return await self.nonces.next_nonce(address=address, fetch_nonce=fetch_nonce)

    async def current_blockhash(self, *, fetch_blockhash: Callable[[], Awaitable[str]]) -> str:
        return await self.blockhash.get_recent_blockhash(fetch_blockhash)

    def on_send_attempt(self, *, chain: str, strategy: str) -> None:
        record_tx_sent(family=_family_for_chain(chain), chain=chain, strategy=strategy)

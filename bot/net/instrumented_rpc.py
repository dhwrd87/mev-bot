from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import aiohttp

from ops.metrics import record_rpc_error, record_rpc_latency


def _provider_name(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "rpc").lower()
    except Exception:
        return "rpc"


def _error_bucket(http_status: Optional[int], err_msg: str) -> str:
    if http_status == 429:
        return "429"
    if http_status is not None:
        if 500 <= int(http_status) <= 599:
            return "5xx"
        if 400 <= int(http_status) <= 499:
            return "4xx"
    msg = (err_msg or "").lower()
    if "timeout" in msg:
        return "timeout"
    if "connect" in msg or "connection" in msg:
        return "conn"
    return "rpc_error"


@dataclass
class RpcCallResult:
    ok: bool
    result: Any | None
    endpoint: str | None
    http_status: int | None
    error_type: str | None
    error_msg: str | None


class AsyncInstrumentedRpcClient:
    def __init__(
        self,
        *,
        urls: Iterable[str],
        family: str,
        chain: str,
        rate_limit_rps: float = 0.0,
    ) -> None:
        self.urls = [str(u).strip() for u in urls if str(u).strip()]
        self.family = str(family or "evm").strip().lower() or "evm"
        self.chain = str(chain or "unknown").strip().lower() or "unknown"
        self._idx = 0
        self._rate_limit_rps = max(0.0, float(rate_limit_rps))
        self._last_call_ts = 0.0
        self._rate_lock = asyncio.Lock()

    def set_context(self, *, family: str, chain: str, urls: Iterable[str] | None = None) -> None:
        self.family = str(family or "evm").strip().lower() or "evm"
        self.chain = str(chain or "unknown").strip().lower() or "unknown"
        if urls is not None:
            self.urls = [str(u).strip() for u in urls if str(u).strip()]
            self._idx = 0

    async def _maybe_rate_limit(self) -> None:
        if self._rate_limit_rps <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            min_interval = 1.0 / self._rate_limit_rps
            sleep_s = (self._last_call_ts + min_interval) - now
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            self._last_call_ts = time.monotonic()

    async def call(
        self,
        sess: aiohttp.ClientSession,
        *,
        method: str,
        params: list[Any] | None = None,
        timeout_s: float = 6.0,
    ) -> RpcCallResult:
        if not self.urls:
            return RpcCallResult(
                ok=False,
                result=None,
                endpoint=None,
                http_status=None,
                error_type="no_rpc",
                error_msg="No RPC endpoints configured",
            )

        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last_err: RpcCallResult | None = None
        for i in range(len(self.urls)):
            await self._maybe_rate_limit()
            idx = (self._idx + i) % len(self.urls)
            url = self.urls[idx]
            provider = _provider_name(url)
            t0 = time.perf_counter()
            try:
                async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                    elapsed = max(0.0, time.perf_counter() - t0)
                    record_rpc_latency(
                        family=self.family,
                        chain=self.chain,
                        provider=provider,
                        method=method,
                        seconds=elapsed,
                    )

                    body = await resp.json()
                    if resp.status == 429:
                        record_rpc_error(
                            provider=provider,
                            code_bucket="429",
                            family=self.family,
                            chain=self.chain,
                        )
                        last_err = RpcCallResult(
                            ok=False,
                            result=None,
                            endpoint=url,
                            http_status=429,
                            error_type="rate_limited",
                            error_msg="HTTP 429",
                        )
                        continue

                    if body.get("error"):
                        msg = body.get("error", {}).get("message", "rpc_error")
                        record_rpc_error(
                            provider=provider,
                            code_bucket=_error_bucket(resp.status, str(msg)),
                            family=self.family,
                            chain=self.chain,
                        )
                        last_err = RpcCallResult(
                            ok=False,
                            result=None,
                            endpoint=url,
                            http_status=resp.status,
                            error_type="rpc_error",
                            error_msg=str(msg),
                        )
                        continue

                    self._idx = (idx + 1) % len(self.urls)
                    return RpcCallResult(
                        ok=True,
                        result=body.get("result"),
                        endpoint=url,
                        http_status=resp.status,
                        error_type=None,
                        error_msg=None,
                    )
            except Exception as e:
                elapsed = max(0.0, time.perf_counter() - t0)
                record_rpc_latency(
                    family=self.family,
                    chain=self.chain,
                    provider=provider,
                    method=method,
                    seconds=elapsed,
                )
                record_rpc_error(
                    provider=provider,
                    code_bucket=_error_bucket(None, str(e)),
                    family=self.family,
                    chain=self.chain,
                )
                last_err = RpcCallResult(
                    ok=False,
                    result=None,
                    endpoint=url,
                    http_status=None,
                    error_type="rpc_exception",
                    error_msg=str(e),
                )
                continue

        return last_err or RpcCallResult(
            ok=False,
            result=None,
            endpoint=self.urls[0],
            http_status=None,
            error_type="no_result",
            error_msg="no result",
        )


# bot/exec/simulator.py
from __future__ import annotations
import json, time, asyncio, random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Protocol

import httpx
from web3 import Web3
from web3.exceptions import ContractLogicError

from bot.exec.exact_output import ExactOutputParams, ExactOutputSwapper, SWAPROUTER_ABI
from bot.sim.provider import SimProvider, SwapSimResult, BundleSimResult
from bot.core.telemetry import (
    # add these to telemetry.py (see bottom snippet)
    sim_single_total, sim_single_success_total, sim_single_fail_total,
    sim_bundle_total, sim_bundle_success_total, sim_bundle_fail_total,
)

class Backoff:
    def __init__(self, base=0.25, factor=2.0, max_delay=2.0, jitter=0.2):
        self.base, self.factor, self.max_delay, self.jitter = base, factor, max_delay, jitter
        self.n = 0
    def next(self) -> float:
        raw = min(self.base * (self.factor ** self.n), self.max_delay)
        self.n += 1
        j = raw * self.jitter
        return max(0.05, raw + random.uniform(-j, j))
    def reset(self): self.n = 0

class BuilderEndpoint(Protocol):
    name: str
    url: str
    kind: str
    method_send_bundle: str
    headers: Optional[Dict[str, str]]


class PreSubmitSimulator(SimProvider):
    """
    - simulate_swap: local eth_call against router (uses current chain state)
    - simulate_bundle: remote builder 'eth_callBundle' to simulate same-block permit+swap
    """
    def __init__(self, w3: Web3, builder_endpoints: List[BuilderEndpoint]):
        self.w3 = w3
        # only endpoints that declare a bundle-call method
        self.builders = [e for e in builder_endpoints if getattr(e, "method_send_bundle", None)]
        self._http = httpx.AsyncClient(http2=False, timeout=httpx.Timeout(6.0, read=6.0))

    async def aclose(self):
        await self._http.aclose()

    async def simulate_swap(self, params: ExactOutputParams, sender: str) -> SwapSimResult:
        """
        Simulate exactOutputSingle only. This requires that the sender already has sufficient
        allowance to the router on-chain; use bundle sim otherwise.
        """
        sim_single_total.inc()
        try:
            router_addr = params.router or getattr(params, "router", None)
            swapper = ExactOutputSwapper(self.w3, router_addr)
            # Decode by calling the function locally (eth_call)
            contract = self.w3.eth.contract(address=self.w3.to_checksum_address(router_addr), abi=SWAPROUTER_ABI)
            fn = contract.functions.exactOutputSingle({
                "tokenIn":   self.w3.to_checksum_address(params.token_in),
                "tokenOut":  self.w3.to_checksum_address(params.token_out),
                "fee":       int(params.fee),
                "recipient": self.w3.to_checksum_address(params.recipient),
                "deadline":  int(params.deadline or params.deadline_s or 0),
                "amountOut": int(params.amount_out_exact or params.amount_out),
                "amountInMaximum": int(params.amount_in_max or params.max_amount_in),
                "sqrtPriceLimitX96": int(params.sqrt_price_limit_x96 or 0),
            })
            amount_in = fn.call({"from": self.w3.to_checksum_address(sender)})
            # Policy check
            if int(amount_in) > int(params.amount_in_max):
                sim_single_fail_total.labels(kind="policy").inc()
                return SwapSimResult(ok=False, amount_in=int(amount_in), reason="amount_in_exceeds_max")
            sim_single_success_total.inc()
            return SwapSimResult(ok=True, amount_in=int(amount_in))
        except ContractLogicError as e:
            # Revert (lack of allowance, price impact, etc.)
            sim_single_fail_total.labels(kind="revert").inc()
            return SwapSimResult(ok=False, amount_in=None, reason=str(e))
        except Exception as e:
            sim_single_fail_total.labels(kind="error").inc()
            return SwapSimResult(ok=False, amount_in=None, reason=f"error:{e}")

    async def simulate_bundle(
        self,
        signed_txs_hex: Sequence[str],
        target_block: Optional[int] = None,
        min_timestamp: Optional[int] = None,
        max_timestamp: Optional[int] = None,
        retries_per_endpoint: int = 1,
    ) -> BundleSimResult:
        """
        Try builder endpoints that expose 'eth_callBundle'. Returns first success.
        """
        sim_bundle_total.inc()
        if not self.builders:
            sim_bundle_fail_total.labels(kind="no_endpoint").inc()
            return BundleSimResult(ok=False, endpoint=None, details={"error":"no builder endpoints configured"})

        errors: List[Tuple[str, Any]] = []
        for ep in self.builders:
            method = getattr(ep, "method_send_bundle", "eth_callBundle")
            headers = ep.headers or {}
            backoff = Backoff()
            for attempt in range(retries_per_endpoint + 1):
                try:
                    params: Dict[str, Any] = {"txs": list(signed_txs_hex)}
                    if target_block is not None:
                        params["blockNumber"] = hex(int(target_block))
                    else:
                        # default to "latest" block + 1 (rough); some relays accept just "latest"
                        try:
                            params["blockNumber"] = hex(self.w3.eth.block_number + 1)
                        except Exception:
                            pass
                    if min_timestamp is not None:
                        params["minTimestamp"] = int(min_timestamp)
                    if max_timestamp is not None:
                        params["maxTimestamp"] = int(max_timestamp)

                    payload = {"jsonrpc":"2.0","id":int(time.time()*1000)%10_000_000,"method":method,"params":[params]}
                    r = await self._http.post(ep.url, headers=headers, content=json.dumps(payload))
                    ok, data = self._parse_jsonrpc(r)
                    if not ok:
                        # retry soft errors
                        if self._is_retryable_error(data):
                            await asyncio.sleep(backoff.next())
                            continue
                        errors.append((ep.name, data))
                        break

                    # Parse results: many relays return list of per-tx sim results
                    # We'll consider success when *no tx* has error/revert and overall returns a bundle hash or success marker.
                    detail = {"endpoint": ep.name, "raw": data}
                    per_tx = self._extract_results_list(data)
                    if per_tx:
                        any_bad = any(self._tx_result_bad(x) for x in per_tx)
                        if any_bad:
                            sim_bundle_fail_total.labels(kind="revert").inc()
                            return BundleSimResult(ok=False, endpoint=ep.name, details=detail)
                    sim_bundle_success_total.inc()
                    return BundleSimResult(ok=True, endpoint=ep.name, details=detail)
                except (httpx.HTTPError, httpx.ReadTimeout) as e:
                    if attempt < retries_per_endpoint:
                        await asyncio.sleep(backoff.next())
                        continue
                    errors.append((ep.name, {"transport": str(e)}))
                    break
        # all failed
        sim_bundle_fail_total.labels(kind="exhausted").inc()
        return BundleSimResult(ok=False, endpoint=None, details={"errors": errors})

    # -------- helpers ----------
    @staticmethod
    def _parse_jsonrpc(resp: httpx.Response) -> Tuple[bool, Any]:
        if resp.status_code == 429:
            return False, {"code":429,"message":"rate limited"}
        if resp.status_code >= 500:
            return False, {"code":resp.status_code,"message":f"http {resp.status_code}"}
        try:
            try:
                data = resp.json()
            except TypeError:
                data = resp.json.__func__()  # type: ignore[attr-defined]
        except Exception:
            text = getattr(resp, "text", "")
            if not text:
                text = repr(resp)
            return False, {"code":-1,"message":f"non-json: {text[:120]}"}
        if "error" in data:
            return False, data["error"]
        return True, data.get("result", data)

    @staticmethod
    def _extract_results_list(data: Any) -> Optional[List[Dict[str, Any]]]:
        # Common shapes: {"bundleHash": "...", "results":[{...}, ...]}
        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list):
                return data["results"]
            if "simResults" in data and isinstance(data["simResults"], list):
                return data["simResults"]
        return None

    @staticmethod
    def _tx_result_bad(x: Dict[str, Any]) -> bool:
        # Heuristics across relays
        if isinstance(x, dict):
            if x.get("error") or x.get("revert") or x.get("reverted"):
                return True
            msg = (x.get("errorMessage") or x.get("revertReason") or "").lower()
            if msg:
                return True
        return False

    @staticmethod
    def _is_retryable_error(err: Dict[str, Any]) -> bool:
        code = err.get("code")
        msg = (err.get("message") or "").lower()
        return code in (429, -32005, -32000) or "timeout" in msg or "temporar" in msg or "rate" in msg or "busy" in msg

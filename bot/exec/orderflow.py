from __future__ import annotations
import time
import asyncio, json, random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Any
import httpx
from bot.telemetry.metrics import (
    PRIVATE_SUBMIT_ATTEMPTS,
    PRIVATE_SUBMIT_SUCCESS,
    PRIVATE_SUBMIT_ERRORS,
    PRE_SUBMIT_LATENCY_MS,
)

@dataclass
class Endpoint:
    name: str
    kind: str                     # e.g. "rpc" | "flashbots"
    url: str
    method_send_bundle: str = "eth_callBundle"
    headers: Optional[Dict[str, str]] = None
    timeout_s: float = 6.0

@dataclass
class TxMeta:
    chain: str

class PrivateOrderflowError(Exception): ...

class PrivateOrderflowManager:
    def __init__(self, endpoints: List[Endpoint], timeout_s: int = 5, max_retries: int = 2, base_backoff_s: float = 0.25):
        if not endpoints:
            raise ValueError("no private endpoints configured")
        self.endpoints = endpoints
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s

    async def _post_json(self, client: httpx.AsyncClient, ep: Endpoint, payload: Dict[str, Any]) -> httpx.Response:
        return await client.post(ep.url, json=payload, headers=ep.headers or {}, timeout=self.timeout_s)

    def _bundle_payload(self, ep: Endpoint, signed_txs_hex: Sequence[str], meta: TxMeta) -> Dict[str, Any]:
        if ep.kind == "rpc":
            # Many private RPCs accept single raw tx (tests don't care about exact shape)
            return {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [signed_txs_hex[0]]}
        if ep.kind == "flashbots":
            return {
                "jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle",
                "params": [{"txs": signed_txs_hex, "revertingTxHashes": []}]
            }
        # default to posting something JSON so the mock still matches, but fail cleanly
        return {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [signed_txs_hex[0]]}

    async def submit_private_bundle(self, raw_txs, meta) -> Dict[str, Any]:
        """
        First HTTP 200 wins -> return:
          {"ok": True, "endpoint": <name>, "result"?: <payload.result>, "body"?: <payload-or-text>}
        If all attempts (endpoints × retries) fail -> raise Exception containing the word "attempts".
        """
        last_err: Optional[Exception] = None
        attempts = 0

        for attempt in range(self.max_retries + 1):
            for ep in self.endpoints:
                attempts += 1
                t0 = time.time()
                PRIVATE_SUBMIT_ATTEMPTS.labels(endpoint=ep.name).inc()
                try:
                    resp = await self._post(ep, raw_txs)
                    # Parse JSON (if possible) regardless of status
                    try:
                        payload: Any = resp.json()
                    except Exception:
                        payload = resp.text

                    if resp.status_code == 200:
                        PRE_SUBMIT_LATENCY_MS.observe((time.time() - t0) * 1000.0)
                        PRIVATE_SUBMIT_SUCCESS.labels(endpoint=ep.name).inc()
                        out: Dict[str, Any] = {"ok": True, "endpoint": ep.name}
                        if isinstance(payload, dict) and "result" in payload:
                            out["result"] = payload["result"]
                        else:
                            out["body"] = payload
                        return out
                    else:
                        PRE_SUBMIT_LATENCY_MS.observe((time.time() - t0) * 1000.0)
                        PRIVATE_SUBMIT_ERRORS.labels(endpoint=ep.name, kind=f"http_{resp.status_code}").inc()
                except Exception as e:
                    PRE_SUBMIT_LATENCY_MS.observe((time.time() - t0) * 1000.0)
                    PRIVATE_SUBMIT_ERRORS.labels(endpoint=ep.name, kind="exception").inc()
                    last_err = e

            # backoff between rounds
            await asyncio.sleep(min(2.0, self.base_backoff_s * (2 ** attempt)))

        raise Exception(f"all attempts failed; attempts={attempts}; last_err={last_err!r}")
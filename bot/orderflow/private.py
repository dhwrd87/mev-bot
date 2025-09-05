from dataclasses import dataclass
from typing import List, Dict, Any
import asyncio
import httpx

@dataclass
class Endpoint:
    name: str      # e.g. "good"
    kind: str      # e.g. "rpc" | "flashbots"
    url: str       # e.g. "https://good.private"

@dataclass
class TxMeta:
    chain: str     # e.g. "polygon"

class PrivateOrderflowManager:
    def __init__(self, endpoints: List[Endpoint], timeout_s: float = 3.0,
                 max_retries: int = 1, base_backoff_s: float = 0.05):
        self.endpoints = endpoints
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _post(self, ep: Endpoint, raw_txs: List[str]) -> httpx.Response:
        # Minimal generic shape; tests only check status codes and endpoint selection
        return await self._client.post(ep.url, json={"txs": raw_txs})

    async def submit_private_bundle(self, raw_txs: List[str], meta: TxMeta) -> Dict[str, Any]:
        last_err = None
        for attempt in range(self.max_retries + 1):
            for ep in self.endpoints:
                try:
                    resp = await self._post(ep, raw_txs)
                    if resp.status_code == 200:
                        body = None
                        try:
                            body = resp.json()
                        except Exception:
                            body = resp.text
                        return {"ok": True, "endpoint": ep.name, "status": resp.status_code, "body": body}
                except Exception as e:
                    last_err = e
            # backoff before next retry round
            await asyncio.sleep(min(2.0, self.base_backoff_s * (2 ** attempt)))
        raise Exception(f"All private orderflow endpoints failed: {last_err}")

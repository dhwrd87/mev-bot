from __future__ import annotations
import asyncio, json, os, random, time, hashlib
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

import aiohttp

from bot.core.util import now_ms
from bot.core.telemetry import (
    bundle_attempts_total, bundle_inclusions_total, bundle_rejections_total,
    bundle_inclusion_latency_ms, builder_success_ratio
)
from bot.core.config import settings

@dataclass
class RawTx:
    """A signed raw transaction hex string."""
    hex: str
    from_addr: Optional[str] = None
    nonce: Optional[int] = None

@dataclass
class Bundle:
    """
    Atomic bundle (target first, then our backrun).
    For some strategies you may want [our frontrun, target, our backrun].
    """
    txs: List[RawTx]
    target_block: int
    min_timestamp: Optional[int] = None
    max_timestamp: Optional[int] = None
    replay_salt: str = ""  # unique replay protection tag

    @staticmethod
    def new(txs: List[RawTx], current_block: int, skew: int, ttl_s: int = 3) -> "Bundle":
        now = int(time.time())
        salt = hashlib.sha256(f"{now}-{random.getrandbits(64)}".encode()).hexdigest()[:16]
        return Bundle(
            txs=txs,
            target_block=current_block + skew,
            min_timestamp=now,
            max_timestamp=now + ttl_s,
            replay_salt=salt
        )

class Backoff:
    def __init__(self, base: float, factor: float, max_d: float, jitter: float):
        self.base, self.factor, self.max_d, self.jitter = base, factor, max_d, jitter
        self.n = 0
    def next(self) -> float:
        d = min(self.base * (self.factor ** self.n), self.max_d)
        self.n += 1
        j = d * self.jitter
        return max(0.05, d + random.uniform(-j, j))
    def reset(self): self.n = 0

# --------- Builder clients (Flashbots-like JSON-RPC) ------------------------

class BuilderClient:
    def __init__(self, name: str, chain: str, url: str):
        self.name, self.chain, self.url = name, chain, url

    async def sim_bundle(self, bundle: Bundle, timeout_ms: int) -> Dict[str, Any]:
        """
        Simulate bundle. We standardize on Flashbots-style simulate API if available.
        In tests, we patch this method to return success/failure.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_callBundle",
            "params": [{
                "txs": [tx.hex for tx in bundle.txs],
                "blockNumber": hex(bundle.target_block),
                "minTimestamp": bundle.min_timestamp,
                "maxTimestamp": bundle.max_timestamp,
                "stateBlockNumber": "latest"
            }]
        }
        return await self._post(payload, timeout_ms)

    async def send_bundle(self, bundle: Bundle, timeout_ms: int) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_sendBundle",
            "params": [{
                "txs": [tx.hex for tx in bundle.txs],
                "blockNumber": hex(bundle.target_block),
                "minTimestamp": bundle.min_timestamp,
                "maxTimestamp": bundle.max_timestamp,
                "replayProtection": {"salt": bundle.replay_salt}  # best-effort generic field
            }]
        }
        return await self._post(payload, timeout_ms)

    async def _post(self, payload: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        to = aiohttp.ClientTimeout(total=timeout_ms / 1000.0)
        headers = {"Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=to) as s:
            async with s.post(self.url, data=json.dumps(payload), headers=headers) as r:
                txt = await r.text()
                try:
                    return json.loads(txt)
                except Exception:
                    return {"error": {"message": f"non_json_response:{r.status}"}}

    @staticmethod
    def is_retryable(errmsg: str) -> bool:
        e = (errmsg or "").lower()
        return any(x in e for x in ["timeout", "temporar", "unavailable", "try again", "rate"])

    @staticmethod
    def classify_reason(errmsg: str) -> str:
        e = (errmsg or "").lower()
        if "nonce" in e: return "nonce"
        if "revert" in e or "simulation" in e: return "simulation"
        if "underpriced" in e or "fee" in e: return "fee"
        if "rate" in e: return "rate"
        if "timeout" in e: return "timeout"
        return "other"

# --------- Multi-builder submitter -----------------------------------------

class BundleSubmitter:
    def __init__(self, chain: str):
        self.chain = chain
        c = settings.chains[chain]
        self.builder_order: List[str] = list(c.builder_order)
        self.builders: Dict[str, BuilderClient] = {}
        for name in self.builder_order:
            url = c.builders[name]["url"]
            self.builders[name] = BuilderClient(name, chain, url)
        bcfg = c.bundle.backoff
        self.backoff_cfg = (float(bcfg.base), float(bcfg.factor), float(bcfg.max), float(bcfg.jitter))
        self.max_retries = int(c.bundle.max_retries_per_builder)
        self.sim_timeout_ms = int(c.bundle.sim_timeout_ms)
        self.submit_timeout_ms = int(c.bundle.submit_timeout_ms)

        # local attempt/inclusion ledger for ratio gauge (optional)
        self._attempts: Dict[str, int] = {name: 0 for name in self.builder_order}
        self._inclusions: Dict[str, int] = {name: 0 for name in self.builder_order}

    async def simulate(self, builder: BuilderClient, bundle: Bundle) -> bool:
        r = await builder.sim_bundle(bundle, self.sim_timeout_ms)
        ok = not r.get("error")
        if not ok:
            reason = builder.classify_reason(r.get("error", {}).get("message",""))
            bundle_rejections_total.labels(builder=builder.name, chain=self.chain, reason=f"sim_{reason}").inc()
        return ok

    async def submit(self, bundle: Bundle) -> Optional[str]:
        """
        Try each builder in order with retry/backoff.
        Returns tx hash (or bundle tag) string on success, None otherwise.
        """
        last_err = None
        for name in self.builder_order:
            client = self.builders[name]
            backoff = Backoff(*self.backoff_cfg)

            # Optional simulation gate
            sim_ok = await self.simulate(client, bundle)
            if not sim_ok:
                continue

            for attempt in range(self.max_retries + 1):
                start = now_ms()
                self._attempts[name] += 1
                bundle_attempts_total.labels(builder=name, chain=self.chain).inc()
                r = await client.send_bundle(bundle, self.submit_timeout_ms)
                latency = max(0.0, now_ms() - start)
                bundle_inclusion_latency_ms.labels(builder=name, chain=self.chain).observe(latency)

                if "result" in r and r["result"]:
                    # Assume success path returns a tag or tx hash
                    self._inclusions[name] += 1
                    bundle_inclusions_total.labels(builder=name, chain=self.chain).inc()
                    self._update_ratio(name)
                    return str(r["result"])

                err = r.get("error", {}).get("message", "unknown")
                last_err = f"{name}:{err}"
                bundle_rejections_total.labels(builder=name, chain=self.chain, reason=client.classify_reason(err)).inc()
                self._update_ratio(name)

                if client.is_retryable(err) and attempt < self.max_retries:
                    await asyncio.sleep(backoff.next())
                    continue
                else:
                    break  # next builder

        return None

    def _update_ratio(self, builder: str):
        att = max(1, self._attempts.get(builder, 0))
        inc = self._inclusions.get(builder, 0)
        builder_success_ratio.labels(builder=builder, chain=self.chain).set(inc / att)

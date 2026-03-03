# bot/net/rpc_client.py
import time, asyncio, random, os
from typing import Any, Dict, Optional, List, Callable, TypeVar
from urllib.parse import urlparse
from web3 import Web3
from web3.types import TxData
from eth_account import Account
from bot.config.knobs import Knobs as K
from ops.metrics import record_rpc_error, record_rpc_latency


def _provider_name(url: str) -> str:
    try:
        h = urlparse(url).hostname
        return str(h or "rpc")
    except Exception:
        return "rpc"

T = TypeVar("T")

class _TokenBucket:
    def __init__(self, rate_qps: float, burst: int):
        self.rate = rate_qps
        self.capacity = burst
        self.tokens = burst
        self.ts = time.perf_counter()
        self.lock = asyncio.Lock()

    async def take(self):
        async with self.lock:
            now = time.perf_counter()
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens < 1.0:
                await asyncio.sleep((1.0 - self.tokens) / self.rate)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0

class _TTL:
    def __init__(self, ms: int): self.ms = ms; self.v=None; self.t=0.0
    def get(self): 
        if (time.perf_counter()*1000 - self.t) < self.ms: return self.v
        return None
    def set(self, v): self.v = v; self.t = time.perf_counter()*1000

class RpcClient:
    def __init__(self, http_url: Optional[str] = None, urls: Optional[List[str]] = None):
        url_list = urls or ([http_url] if http_url else None) or K.RPC_URLS or []
        self.urls = [u for u in url_list if u]
        if not self.urls and K.RPC_HTTP:
            self.urls = [K.RPC_HTTP]
        self._idx = 0
        self.w3s = [Web3(Web3.HTTPProvider(u)) for u in self.urls]
        self.bucket = _TokenBucket(K.HTTP_MAX_QPS, K.HTTP_BURST)
        self.ttl_gas = _TTL(K.GASPRICE_TTL_MS)
        self.ttl_block = _TTL(K.LATEST_BLOCK_TTL_MS)
        self.ttl_nonce: Dict[str,_TTL] = {}
        self.sem = asyncio.Semaphore(int(os.getenv("MEMPOOL_CONCURRENCY", "10")))

        # rudimentary counters (hook into your metrics if you’d like)
        self.calls = {"eth_getTransactionByHash":0, "eth_gasPrice":0, "eth_block":0, "eth_getTransactionCount":0}

    async def _lim(self): await self.bucket.take()

    def _is_429(self, err: Exception) -> bool:
        msg = str(err).lower()
        return "429" in msg or "too many request" in msg

    def _error_bucket(self, err: Exception) -> str:
        msg = str(err).lower()
        if "429" in msg or "too many" in msg or "rate limit" in msg:
            return "429"
        if "timeout" in msg:
            return "timeout"
        if "connect" in msg or "connection" in msg:
            return "conn"
        if " 5" in msg or "500" in msg or "502" in msg or "503" in msg or "504" in msg:
            return "5xx"
        if " 4" in msg or "400" in msg or "401" in msg or "403" in msg or "404" in msg:
            return "4xx"
        return "rpc_error"

    async def _call_with_fallback(self, fn: Callable[[Web3], T]) -> T:
        if not self.w3s:
            raise RuntimeError("No RPC HTTP endpoints configured")
        backoff = 0.2
        attempts = max(1, len(self.w3s))
        for i in range(attempts):
            idx = (self._idx + i) % len(self.w3s)
            w3 = self.w3s[idx]
            t0 = time.perf_counter()
            try:
                result = fn(w3)
                record_rpc_latency(
                    family=os.getenv("CHAIN_FAMILY", "evm"),
                    chain=os.getenv("CHAIN", "unknown"),
                    provider=_provider_name(self.urls[idx] if idx < len(self.urls) else f"rpc_{idx}"),
                    seconds=max(0.0, time.perf_counter() - t0),
                )
                self._idx = (idx + 1) % len(self.w3s)
                return result
            except Exception as e:
                provider = _provider_name(self.urls[idx] if idx < len(self.urls) else f"rpc_{idx}")
                record_rpc_latency(
                    family=os.getenv("CHAIN_FAMILY", "evm"),
                    chain=os.getenv("CHAIN", "unknown"),
                    provider=provider,
                    seconds=max(0.0, time.perf_counter() - t0),
                )
                record_rpc_error(provider=provider, code_bucket=self._error_bucket(e))
                if self._is_429(e):
                    await asyncio.sleep(backoff + random.random() * 0.1)
                    backoff = min(backoff * 2, 2.0)
                    continue
                # fall back to next endpoint on non-429
                continue
        # advance starting index for next call
        self._idx = (self._idx + 1) % len(self.w3s)
        raise RuntimeError("RPC call failed across all endpoints")

    async def get_tx(self, tx_hash: str) -> Optional[TxData]:
        async with self.sem:
            await self._lim()
            self.calls["eth_getTransactionByHash"] += 1
            try:
                return await self._call_with_fallback(lambda w3: w3.eth.get_transaction(tx_hash))
            except Exception:
                return None

    async def gas_price(self) -> int:
        v = self.ttl_gas.get()
        if v is not None: return v
        async with self.sem:
            await self._lim(); self.calls["eth_gasPrice"] += 1
            v = await self._call_with_fallback(lambda w3: w3.eth.gas_price)
            self.ttl_gas.set(int(v))
            return int(v)

    async def latest_block(self) -> Any:
        v = self.ttl_block.get()
        if v is not None: return v
        async with self.sem:
            await self._lim(); self.calls["eth_block"] += 1
            b = await self._call_with_fallback(lambda w3: w3.eth.get_block("latest"))
            self.ttl_block.set(b)
            return b

    async def nonce(self, addr: str) -> int:
        key = addr.lower()
        if key not in self.ttl_nonce: self.ttl_nonce[key] = _TTL(K.NONCE_TTL_MS)
        v = self.ttl_nonce[key].get()
        if v is not None: return v
        async with self.sem:
            await self._lim(); self.calls["eth_getTransactionCount"] += 1
            n = await self._call_with_fallback(lambda w3: w3.eth.get_transaction_count(addr))
            self.ttl_nonce[key].set(int(n))
            return int(n)

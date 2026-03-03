# bot/data/pools_v2.py
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Dict, Tuple, Optional
from web3 import Web3

V2_FACTORY_ABI = [
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},
               {"internalType":"address","name":"tokenB","type":"address"}],
     "name":"getPair","outputs":[{"internalType":"address","name":"pair","type":"address"}],
     "stateMutability":"view","type":"function"}
]

V2_PAIR_ABI = [
    {"inputs":[],"name":"getReserves","outputs":[
        {"internalType":"uint112","name":"reserve0","type":"uint112"},
        {"internalType":"uint112","name":"reserve1","type":"uint112"},
        {"internalType":"uint32","name":"blockTimestampLast","type":"uint32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"fee","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"}  # not standard on all forks; we’ll default to 30 bps if missing
]

@dataclass
class V2PoolInfo:
    pair: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    fee_bps: int

class V2PoolFetcher:
    """
    Minimal on-chain fetcher with a TTL cache to avoid hammering RPC.
    """
    def __init__(self, w3: Web3, factory_address: str, ttl_seconds: int = 5):
        self.w3 = w3
        self._factory_address = self._safe_checksum(factory_address)
        self.factory = None
        self.ttl = ttl_seconds
        self._pair_cache: Dict[Tuple[str, str], str] = {}
        self._pool_cache: Dict[str, Tuple[float, V2PoolInfo]] = {}

    def _factory(self):
        if self.factory is None:
            self.factory = self.w3.eth.contract(address=self._factory_address, abi=V2_FACTORY_ABI)
        return self.factory

    def _sorted_pair(self, a: str, b: str) -> Tuple[str, str]:
        ca, cb = self._safe_checksum(a), self._safe_checksum(b)
        return (ca, cb) if ca.lower() < cb.lower() else (cb, ca)

    @staticmethod
    def _safe_checksum(addr: str) -> str:
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) < 42:
            addr = "0x" + addr[2:].rjust(40, "0")
        return Web3.to_checksum_address(addr)

    def get_pair(self, token_a: str, token_b: str) -> Optional[str]:
        key = self._sorted_pair(token_a, token_b)
        if key in self._pair_cache:
            return self._pair_cache[key]
        addr = self._factory().functions.getPair(key[0], key[1]).call()
        if int(addr, 16) == 0:
            return None
        self._pair_cache[key] = self._safe_checksum(addr)
        return self._pair_cache[key]

    def get_pool_info(self, token_in: str, token_out: str) -> Optional[V2PoolInfo]:
        pair = self.get_pair(token_in, token_out)
        if not pair:
            return None
        now = time.time()
        cached = self._pool_cache.get(pair)
        if cached and now - cached[0] < self.ttl:
            return cached[1]

        c = self.w3.eth.contract(address=pair, abi=V2_PAIR_ABI)
        t0 = c.functions.token0().call()
        t1 = c.functions.token1().call()
        r0, r1, _ = c.functions.getReserves().call()
        # Fee bps: try .fee() if available, else 30 bps (0.3%)
        try:
            fee_raw = c.functions.fee().call()
            fee_bps = int(fee_raw) if int(fee_raw) < 10000 else 30
        except Exception:
            fee_bps = 30

        info = V2PoolInfo(
            pair=self._safe_checksum(pair),
            token0=self._safe_checksum(t0),
            token1=self._safe_checksum(t1),
            reserve0=int(r0),
            reserve1=int(r1),
            fee_bps=fee_bps
        )
        self._pool_cache[pair] = (now, info)
        return info

    def reserves_aligned(self, token_in: str, token_out: str) -> Optional[Tuple[int, int, int]]:
        """
        Returns (reserve_in, reserve_out, fee_bps) aligned to token_in/token_out direction.
        """
        info = self.get_pool_info(token_in, token_out)
        if not info:
            return None
        if self._safe_checksum(token_in) == info.token0 and self._safe_checksum(token_out) == info.token1:
            return info.reserve0, info.reserve1, info.fee_bps
        elif self._safe_checksum(token_in) == info.token1 and self._safe_checksum(token_out) == info.token0:
            return info.reserve1, info.reserve0, info.fee_bps
        return None

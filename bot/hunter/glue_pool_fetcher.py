# bot/hunter/glue_pool_fetcher.py
from __future__ import annotations
from typing import Tuple
from web3 import Web3

from bot.data.pools_v2 import V2PoolFetcher

class PoolFetcher:
    def __init__(self, w3: Web3, factory_address: str, ttl_seconds: int = 5):
        self.v2 = V2PoolFetcher(w3, factory_address, ttl_seconds)

    def reserves_fetcher(self, token_in: str, token_out: str, fee_tier: int | None) -> Tuple[int, int, int, float]:
        """
        Returns (reserve_in, reserve_out, fee_bps, price_usd_out)
        For MVP we return price_usd_out = 1.0 (caller can overlay real prices).
        """
        aligned = self.v2.reserves_aligned(token_in, token_out)
        if not aligned:
            raise RuntimeError("No V2 pool for token pair")
        reserve_in, reserve_out, fee_bps = aligned
        return reserve_in, reserve_out, fee_bps, 1.0  # plug your pricing later

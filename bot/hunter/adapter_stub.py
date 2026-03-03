class StaticPricingAdapter:
    """
    Test/placeholder adapter: returns fixed ETH/USD and defers to calculator's approximations.
    """
    def __init__(self, eth_usd: float = 2500.0):
        self._eth_usd = eth_usd

    async def estimate_v2_out(self, token_in, token_out, amount_in, fee_bps):  # not used in stub path
        raise NotImplementedError

    async def estimate_v3_out(self, token_in, token_out, amount_in, fee_bps):
        raise NotImplementedError

    async def get_eth_usd(self, chain: str) -> float:
        return self._eth_usd

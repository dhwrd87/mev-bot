import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.route.selector import RouteSelector
from bot.quote.v3_quoter import V3Quoter, V3QuoteOut

class DummyQuoter(V3Quoter):
    def __init__(self): pass
    def quote_exact_output_single(self, token_in, token_out, want_out, fee, sqrt_price_limit_x96=0):
        # Make 3000 tier cheaper than V2; others worse
        if fee == 3000:
            return V3QuoteOut(ok=True, amount_in=900, gas_estimate=120_000)
        return V3QuoteOut(ok=True, amount_in=2000, gas_estimate=140_000)

def test_route_selector_prefers_v3_when_cheaper():
    sel = RouteSelector(DummyQuoter(), fee_tiers=(500,3000,10000))
    # V2 requires 1000 in; v3(3000) requires 900 -> choose v3
    choice = sel.choose_for_exact_out("0x1","0x2", want_out=1000, v2_reserves=(10**9, 10**9, 30), gas_penalty=None)
    assert choice.router_kind == "v3" and choice.fee_or_fee_bps == 3000 and choice.amount_in == 900

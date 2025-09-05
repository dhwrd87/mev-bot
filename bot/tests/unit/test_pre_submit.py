import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.sim.pre_submit import PreSubmitSimulator
from bot.quote.v3_quoter import V3Quoter, V3QuoteOut

class DummyQuoter(V3Quoter):
    def __init__(self): pass
    def quote_exact_output_single(self, *_args, **_kw):
        return V3QuoteOut(ok=True, amount_in=900, gas_estimate=150_000)

async def test_best_of_prefers_lower_need_in():
    w3 = Web3(EthereumTesterProvider())
    sim = PreSubmitSimulator(w3, DummyQuoter())
    v2, v3 = sim.best_of("0x1","0x2", want_out=1000, v2_reserves=(10**9,10**9,30), v3_fee=3000)
    assert v2.ok and v3.ok
    assert v3.need_in < v2.need_in

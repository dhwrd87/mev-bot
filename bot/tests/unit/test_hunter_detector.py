import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.hunter.decoder import PendingTxView
from bot.hunter.detector import SniperDetector

pytestmark = pytest.mark.asyncio

def _w3():
    return Web3(EthereumTesterProvider())

def fake_pool_fetcher(token_in, token_out, fee_tier):
    # reserves big enough to produce impact for amount_in=1e18 with 0.3% fee
    reserve_in = 10**21
    reserve_out = 10**21
    fee_bps = 30
    price_usd_out = 1.0
    return reserve_in, reserve_out, fee_bps, price_usd_out

async def test_flags_high_tip_impact():
    w3 = _w3()
    # minimal v2 data to pass decode through fake path: we won't decode here, we craft a compatible payload
    from bot.hunter.decoder import TxDecoder
    v2 = w3.eth.contract(address="0x0000000000000000000000000000000000009999", abi=TxDecoder(w3)._v2("0x0000000000000000000000000000000000009999").abi)
    data = v2.encodeABI(fn_name="swapExactTokensForTokens", args=[
        10**18, 1, ["0x0000000000000000000000000000000000000001","0x0000000000000000000000000000000000000004"], w3.eth.accounts[0], 9999999999
    ])
    txv = PendingTxView(
        hash="0xdead", to=v2.address, from_=w3.eth.accounts[1],
        max_fee_per_gas=None, max_priority_fee_per_gas=int(20e9), gas_price_legacy=None, input=data
    )
    detector = SniperDetector(w3, stable_tokens={"0x0000000000000000000000000000000000000003"}, gas_tip_threshold_gwei=5, impact_bps_threshold=50)
    opp = detector.estimate(txv, fake_pool_fetcher)
    assert opp is not None
    assert opp.est_price_impact_bps >= 50

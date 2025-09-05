import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.hunter.decoder import TxDecoder, PendingTxView

pytestmark = pytest.mark.asyncio

def _w3():
    w3 = Web3(EthereumTesterProvider())
    return w3

async def test_decode_v2_swap_exact_tokens():
    w3 = _w3()
    # Build calldata by encoding the ABI
    v2 = w3.eth.contract(address="0x0000000000000000000000000000000000009999", abi=[
        {"name":"swapExactTokensForTokens","type":"function","stateMutability":"nonpayable",
         "inputs":[
            {"name":"amountIn","type":"uint256"},
            {"name":"amountOutMin","type":"uint256"},
            {"name":"path","type":"address[]"},
            {"name":"to","type":"address"},
            {"name":"deadline","type":"uint256"}],
         "outputs":[{"name":"amounts","type":"uint256[]"}]},
    ])
    data = v2.encodeABI(fn_name="swapExactTokensForTokens", args=[
        10**18, 1, ["0x0000000000000000000000000000000000000001","0x0000000000000000000000000000000000000002"], w3.eth.accounts[0], 9999999999
    ])
    txv = PendingTxView(hash="0xaaa", to=v2.address, from_=w3.eth.accounts[1], max_fee_per_gas=None, max_priority_fee_per_gas=20_000_000_000, gas_price_legacy=None, input=data)
    dec = TxDecoder(w3)
    s = dec.decode_swap(txv)
    assert s and s.kind == "v2" and s.token_in.lower().endswith("1") and s.token_out.lower().endswith("2")

async def test_decode_v3_exact_input_single():
    w3 = _w3()
    v3 = w3.eth.contract(address="0x0000000000000000000000000000000000008888", abi=[
        {"name":"exactInputSingle","type":"function","stateMutability":"payable",
         "inputs":[{"name":"params","type":"tuple","components":[
            {"name":"tokenIn","type":"address"},
            {"name":"tokenOut","type":"address"},
            {"name":"fee","type":"uint24"},
            {"name":"recipient","type":"address"},
            {"name":"deadline","type":"uint256"},
            {"name":"amountIn","type":"uint256"},
            {"name":"amountOutMinimum","type":"uint256"},
            {"name":"sqrtPriceLimitX96","type":"uint160"}]}],
         "outputs":[{"name":"amountOut","type":"uint256"}]},
    ])
    data = v3.encodeABI(fn_name="exactInputSingle", args=[(
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
        3000, "0x0000000000000000000000000000000000000003",
        9999999999, 10**18, 1, 0
    )])
    txv = PendingTxView(hash="0xbbb", to=v3.address, from_=w3.eth.accounts[1], max_fee_per_gas=None, max_priority_fee_per_gas=30_000_000_000, gas_price_legacy=None, input=data)
    dec = TxDecoder(w3)
    s = dec.decode_swap(txv)
    assert s and s.kind == "v3" and s.fee_tier == 3000

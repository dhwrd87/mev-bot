import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.exec.v2_swapper import V2ExactOutputSwapper, V2SwapParams

def test_v2_build_swap_tx():
    w3 = Web3(EthereumTesterProvider())
    router = "0x0000000000000000000000000000000000009999"
    swapper = V2ExactOutputSwapper(w3, router)
    tx = swapper.build_swap_tx(
        V2SwapParams(
            router=router, token_in="0x0000000000000000000000000000000000000001",
            token_out="0x0000000000000000000000000000000000000002",
            amount_out_exact=12345, amount_in_max=67890,
            recipient=w3.eth.accounts[0], deadline=9999999999
        ),
        sender=w3.eth.accounts[0]
    )
    assert tx["to"].lower() == router.lower()
    assert "data" in tx and len(tx["data"]) > 10

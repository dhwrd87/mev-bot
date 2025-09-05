import pytest, time
from web3 import Web3
from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams

@pytest.fixture
def w3():
    # eth_tester provider gets installed in your image; OK for encode+call stubs.
    return Web3(Web3.EthereumTesterProvider())

def _addr(n):  # silly pseudo-address helper for tests
    return Web3.to_checksum_address("0x" + f"{n:040x}")

def test_builds_calldata_shape(w3):
    swapper = ExactOutputSwapper(w3, router=_addr(1))
    p = ExactOutputParams(
        token_in=_addr(2),
        token_out=_addr(3),
        fee=3000,
        recipient=_addr(4),
        amount_out=10**6,
        max_amount_in=10**7,
        deadline_s=600,
        from_addr=_addr(5),
    )
    to, data, value = swapper.build_calldata(p)
    assert to == _addr(1)
    assert isinstance(data, (bytes, bytearray)) and len(data) > 4
    assert value == 0

def test_slippage_guard_rejects(w3):
    swapper = ExactOutputSwapper(w3, router=_addr(1))
    p = ExactOutputParams(
        token_in=_addr(2), token_out=_addr(3), fee=3000,
        recipient=_addr(4), amount_out=1000, max_amount_in=1200,
        quote_amount_in=1000, max_slippage_bps=100,  # 1% → allowed = 1010
    )
    with pytest.raises(ValueError):
        swapper.build_calldata(p)

def test_simulation_catches_revert(monkeypatch, w3):
    swapper = ExactOutputSwapper(w3, router=_addr(1))
    p = ExactOutputParams(
        token_in=_addr(2), token_out=_addr(3), fee=3000,
        recipient=_addr(4), amount_out=1000, max_amount_in=2000,
    )
    # Force a revert by monkeypatching eth.call
    def fake_call(_):
        raise ValueError({"data": {"0x": {"reason": "UniswapV3: INSUFFICIENT_INPUT_AMOUNT"}}})
    monkeypatch.setattr(w3.eth, "call", fake_call)
    ok, err = swapper.simulate(p)
    assert not ok and "INSUFFICIENT_INPUT_AMOUNT" in err

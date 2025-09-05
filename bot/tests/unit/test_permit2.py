import asyncio, time
import pytest
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from bot.exec.permit2 import Permit2Handler, InMemoryNonceStore, PermitParams, PERMIT2_ADDRESS

pytestmark = pytest.mark.asyncio

class DummyContract:
    # Mimic Permit2.allowance(...)->(amount, expiration, nonce)
    def __init__(self, nonce=0):
        self._nonce = nonce
    def functions(self): return self
    @property
    def functions(self): return self
    def allowance(self, owner, token, spender): return self
    def call(self): return (0, 0, self._nonce)

class DummyHandler(Permit2Handler):
    # Replace actual contract with dummy to avoid chain calls
    def __init__(self, w3, store, verifying_contract=None, start_nonce=0):
        super().__init__(w3, store, verifying_contract)
        self.contract = DummyContract(nonce=start_nonce)

def _w3():
    w3 = Web3(EthereumTesterProvider())
    # Chain id 131277322940537 (eth_tester) → override to 137 for deterministic tests
    w3.eth.chain_id = 137
    return w3

def _params():
    now = int(time.time())
    return PermitParams(
        owner=Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD"),
        token=Web3.to_checksum_address("0x0000000000000000000000000000000000000001"),
        spender=Web3.to_checksum_address("0x0000000000000000000000000000000000000002"),
        amount=10**6,
        expiration=now + 3600,
        sig_deadline=now + 300
    )

async def test_domain_and_primary_type():
    h = DummyHandler(_w3(), InMemoryNonceStore(), PERMIT2_ADDRESS, start_nonce=7)
    td = await h.build_typed_data(_params())
    assert td["primaryType"] == "PermitSingle"
    assert td["domain"]["name"] == "Permit2"
    assert td["domain"]["verifyingContract"] == PERMIT2_ADDRESS
    assert td["domain"]["chainId"] == 137

async def test_expiration_and_deadline_within_limits():
    h = DummyHandler(_w3(), InMemoryNonceStore(), PERMIT2_ADDRESS, start_nonce=0)
    td = await h.build_typed_data(_params())
    details = td["message"]["details"]
    assert 0 < details["expiration"] < (1 << 48)   # uint48 clamp
    assert td["message"]["sigDeadline"] > int(time.time())

async def test_nonce_persistence_increment():
    store = InMemoryNonceStore()
    h = DummyHandler(_w3(), store, PERMIT2_ADDRESS, start_nonce=5)
    p = _params()
    res = await h.sign(p, owner_private_key_hex="0x"+"11"*32)  # dummy key
    assert res["nonce_used"] == 5
    # next call uses nonce 6
    td2 = await h.build_typed_data(p)
    assert td2["message"]["details"]["nonce"] == 6

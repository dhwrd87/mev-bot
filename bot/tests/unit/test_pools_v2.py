import pytest, time
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from bot.data.pools_v2 import V2PoolFetcher

pytestmark = pytest.mark.asyncio

async def test_cache_paths(monkeypatch):
    w3 = Web3(EthereumTesterProvider())
    f = V2PoolFetcher(w3, "0x0000000000000000000000000000000000000001", ttl_seconds=1)

    # stub factory.getPair and pair contract calls
    class FFactory:
        def __init__(self): pass
        class functions:
            @staticmethod
            def getPair(a,b):
                class C: 
                    def call(self_): return "0x0000000000000000000000000000000000009999"
                return C()
    class PPair:
        def __init__(self): pass
        class functions:
            @staticmethod
            def token0():
                class C: 
                    def call(self_): return "0x0000000000000000000000000000000000000001"
                return C()
            @staticmethod
            def token1():
                class C:
                    def call(self_): return "0x0000000000000000000000000000000000000002"
                return C()
            @staticmethod
            def getReserves():
                class C:
                    def call(self_): return (1000, 2000, 0)
                return C()
            @staticmethod
            def fee():
                class C:
                    def call(self_): return 30
                return C()

    def fake_contract(address, abi):
        if address.lower().endswith("0001"):
            return FFactory()
        return PPair()

    monkeypatch.setattr(w3.eth, "contract", fake_contract)

    info1 = f.get_pool_info("0x1","0x2")
    assert info1 and info1.reserve0 == 1000
    # second call within TTL must hit cache
    info2 = f.get_pool_info("0x1","0x2")
    assert info2 is info1
    # after TTL, refetch
    time.sleep(1.1)
    info3 = f.get_pool_info("0x1","0x2")
    assert info3 is not None

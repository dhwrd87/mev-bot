from __future__ import annotations

import json

from adapters.dex_packs.evm_univ3 import EVMUniV3Pack
from adapters.dex_packs.evm_univ2 import min_out_from_slippage
from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.types_dex import TradeIntent


class _Call:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _QuoterFns:
    def __init__(self, single_by_fee=None, multi_out=None):
        self.single_by_fee = single_by_fee or {}
        self.multi_out = multi_out

    def quoteExactInputSingle(self, token_in, token_out, fee, amount_in, sqrt_limit):  # noqa: N802
        _ = (token_in, token_out, amount_in, sqrt_limit)
        val = self.single_by_fee.get(int(fee))
        if val is None:
            raise Exception("reverted")
        return _Call(val)

    def quoteExactInput(self, path, amount_in):  # noqa: N802
        _ = (path, amount_in)
        if self.multi_out is None:
            raise Exception("reverted")
        return _Call(self.multi_out)


class _Router:
    def __init__(self):
        self.last_fn = None
        self.last_args = None

    def encodeABI(self, fn_name=None, args=None, **_kwargs):
        self.last_fn = fn_name
        self.last_args = args
        return "0xdeadbeef"


class _Eth:
    def __init__(self, ok=True):
        self.ok = ok

    def call(self, _tx):
        if not self.ok:
            raise Exception("execution reverted: bad_v3")
        return b""

    def estimate_gas(self, _tx):
        return 250000


class _W3:
    def __init__(self, ok=True):
        self.eth = _Eth(ok=ok)

    @staticmethod
    def to_checksum_address(addr):
        return addr


def test_quote_selects_best_fee_tier(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x0000000000000000000000000000000000000001",
            "swap_router": "0x0000000000000000000000000000000000000002",
            "fee_tiers": [500, 3000, 10000],
            "rpc_http": "http://rpc",
        },
        instance_name="uniswapv3",
    )
    q = type("Q", (), {"functions": _QuoterFns(single_by_fee={500: 900, 3000: 1100, 10000: 800})})()
    monkeypatch.setattr(pack, "_quoter_contract", lambda: q)

    intent = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000b1",
        amount_in=1000,
        slippage_bps=100,
        ttl_s=60,
        strategy="default",
    )
    quote = pack.quote(intent)
    assert quote.expected_out == 1100
    assert quote.min_out == min_out_from_slippage(1100, 100)
    assert quote.route_summary.endswith("@3000")


def test_quote_multihop_encoded_path(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x0000000000000000000000000000000000000001",
            "swap_router": "0x0000000000000000000000000000000000000002",
            "paths": {
                "0x00000000000000000000000000000000000000a1->0x00000000000000000000000000000000000000c1": {
                    "tokens": [
                        "0x00000000000000000000000000000000000000a1",
                        "0x00000000000000000000000000000000000000b1",
                        "0x00000000000000000000000000000000000000c1",
                    ],
                    "fees": [500, 3000],
                }
            },
            "rpc_http": "http://rpc",
        },
        instance_name="uniswapv3",
    )
    q = type("Q", (), {"functions": _QuoterFns(multi_out=7777)})()
    monkeypatch.setattr(pack, "_quoter_contract", lambda: q)

    intent = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000c1",
        amount_in=2000,
        slippage_bps=50,
        ttl_s=60,
        strategy="default",
    )
    quote = pack.quote(intent)
    assert quote.expected_out == 7777
    assert "@500" in quote.route_summary and "@3000" in quote.route_summary


def test_build_single_and_multihop(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x0000000000000000000000000000000000000001",
            "swap_router": "0x0000000000000000000000000000000000000002",
            "fee_tiers": [500],
            "rpc_http": "http://rpc",
        },
        instance_name="uniswapv3",
    )
    router = _Router()
    monkeypatch.setattr(pack, "_router_contract", lambda: router)
    intent = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000b1",
        amount_in=5000,
        slippage_bps=100,
        ttl_s=60,
        strategy="default",
    )
    quote = type("Quote", (), {"min_out": 4500, "route_summary": f"{intent.token_in}->{intent.token_out}@500"})()
    plan = pack.build(intent, quote)
    assert plan.raw_tx == "0xdeadbeef"
    assert router.last_fn == "exactInputSingle"
    assert plan.metadata["method"] == "exactInputSingle"

    pack2 = EVMUniV3Pack(
        config={
            "quoter": "0x0000000000000000000000000000000000000001",
            "swap_router": "0x0000000000000000000000000000000000000002",
            "paths": {
                "0x00000000000000000000000000000000000000a1->0x00000000000000000000000000000000000000c1": {
                    "tokens": [
                        "0x00000000000000000000000000000000000000a1",
                        "0x00000000000000000000000000000000000000b1",
                        "0x00000000000000000000000000000000000000c1",
                    ],
                    "fees": [500, 3000],
                }
            },
            "rpc_http": "http://rpc",
        },
        instance_name="uniswapv3",
    )
    router2 = _Router()
    monkeypatch.setattr(pack2, "_router_contract", lambda: router2)
    intent2 = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000c1",
        amount_in=5000,
        slippage_bps=100,
        ttl_s=60,
        strategy="default",
    )
    quote2 = type("Quote", (), {"min_out": 4200, "route_summary": "multi"})()
    plan2 = pack2.build(intent2, quote2)
    assert plan2.raw_tx == "0xdeadbeef"
    assert router2.last_fn == "exactInput"
    assert plan2.metadata["method"] == "exactInput"


def test_simulate_revert(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x0000000000000000000000000000000000000001",
            "swap_router": "0x0000000000000000000000000000000000000002",
            "rpc_http": "http://rpc",
        },
        instance_name="uniswapv3",
    )
    monkeypatch.setattr(pack, "w3", _W3(ok=False))
    plan = type(
        "Plan",
        (),
        {
            "family": "evm",
            "chain": "sepolia",
            "metadata": {"to": "0x0000000000000000000000000000000000000002", "recipient": "0x00000000000000000000000000000000000000ff"},
            "raw_tx": "0xdead",
        },
    )()
    sim = pack.simulate(plan)
    assert sim.ok is False
    assert sim.error_code == "revert"


def test_registry_loads_univ3_instances(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sepolia.yaml").write_text(
        json.dumps(
            {
                "enabled_dex_packs": ["uniswapv3_main", "uniswapv3_alt"],
                "dex_packs": {
                    "uniswapv3_main": {
                        "type": "evm_univ3",
                        "quoter": "0x0000000000000000000000000000000000000001",
                        "swap_router": "0x0000000000000000000000000000000000000002",
                    },
                    "uniswapv3_alt": {
                        "type": "univ3",
                        "quoter": "0x0000000000000000000000000000000000000003",
                        "swap_router": "0x0000000000000000000000000000000000000004",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    reg = DEXPackRegistry(operator_state_path=str(tmp_path / "op_state.json"))
    reg.reload(family="evm", chain="sepolia", network="testnet")
    assert reg.enabled_names() == ["uniswapv3_alt", "uniswapv3_main"]

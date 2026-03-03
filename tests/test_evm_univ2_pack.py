from __future__ import annotations

import json

from adapters.dex_packs.evm_univ2 import EVMUniV2Pack, calc_amount_out, min_out_from_slippage
from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.types_dex import Quote, TradeIntent, TxPlan


class _Call:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _Fns:
    def __init__(self, mapping):
        self._mapping = mapping

    def __getattr__(self, name):
        def _fn(*args):
            key = (name, tuple(str(a).lower() for a in args))
            if key in self._mapping:
                return _Call(self._mapping[key])
            key = (name, ())
            if key in self._mapping:
                return _Call(self._mapping[key])
            raise KeyError(f"missing mock for {name} args={args}")

        return _fn


class _Contract:
    def __init__(self, mapping):
        self.functions = _Fns(mapping)

    def encodeABI(self, fn_name=None, args=None, **_kwargs):
        return "0xabcdef"


class _Eth:
    def __init__(self, call_ok=True):
        self._call_ok = call_ok

    def call(self, _tx):
        if not self._call_ok:
            raise Exception("execution reverted: bad_swap")
        return b""

    def estimate_gas(self, _tx):
        return 210000


class _W3:
    def __init__(self, call_ok=True):
        self.eth = _Eth(call_ok=call_ok)

    @staticmethod
    def to_checksum_address(addr):
        return addr


def test_amount_out_and_min_out_math():
    out = calc_amount_out(amount_in=1000, reserve_in=100_000, reserve_out=50_000, fee_bps=30)
    assert out > 0
    assert out < 500  # less than no-impact amount
    assert min_out_from_slippage(out, 50) == int(out * 9950 // 10000)


def test_quote_uses_factory_pair_reserves(monkeypatch):
    pack = EVMUniV2Pack(
        config={
            "factory": "0xfactory",
            "router": "0xrouter",
            "fee_bps": 30,
            "rpc_http": "http://rpc",
        },
        instance_name="univ2_sushi",
    )
    pair_addr = "0x00000000000000000000000000000000000000aa"
    token_in = "0x00000000000000000000000000000000000000a1"
    token_out = "0x00000000000000000000000000000000000000b1"
    factory = _Contract({("getPair", (token_in, token_out)): pair_addr})
    pair = _Contract(
        {
            ("getReserves", ()): (1_000_000, 2_000_000, 0),
            ("token0", ()): token_in,
            ("token1", ()): token_out,
        }
    )
    monkeypatch.setattr(pack, "_factory_contract", lambda: factory)
    monkeypatch.setattr(pack, "_pair_contract", lambda _addr: pair)

    intent = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in=token_in,
        token_out=token_out,
        amount_in=10_000,
        slippage_bps=100,
        ttl_s=60,
        strategy="default",
        dex_preference="univ2_sushi",
    )
    q = pack.quote(intent)
    assert q.dex == "univ2_sushi"
    assert q.expected_out > 0
    assert q.min_out == min_out_from_slippage(q.expected_out, 100)
    assert "->" in q.route_summary


def test_build_and_simulate(monkeypatch):
    pack = EVMUniV2Pack(
        config={"factory": "0xfactory", "router": "0xrouter", "rpc_http": "http://rpc"},
        instance_name="univ2_sushi",
    )
    monkeypatch.setattr(pack, "_router_contract", lambda: _Contract({}))
    monkeypatch.setattr(pack, "w3", _W3(call_ok=True))

    intent = TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000b1",
        amount_in=10_000,
        slippage_bps=100,
        ttl_s=60,
        strategy="default",
    )
    quote = Quote(
        dex="univ2_sushi",
        expected_out=9_000,
        min_out=8_910,
        price_impact_bps=20.0,
        fee_estimate=30.0,
        route_summary="0x00000000000000000000000000000000000000a1->0x00000000000000000000000000000000000000b1",
        quote_latency_ms=1.0,
    )
    plan = pack.build(intent, quote)
    assert isinstance(plan, TxPlan)
    assert plan.raw_tx == "0xabcdef"
    sim = pack.simulate(plan)
    assert sim.ok is True
    assert sim.gas_estimate == 210000


def test_simulate_revert_parses_reason(monkeypatch):
    pack = EVMUniV2Pack(
        config={"factory": "0xfactory", "router": "0xrouter", "rpc_http": "http://rpc"},
        instance_name="univ2_sushi",
    )
    monkeypatch.setattr(pack, "w3", _W3(call_ok=False))
    plan = TxPlan(
        family="evm",
        chain="sepolia",
        dex="univ2_sushi",
        value=0,
        raw_tx="0xabc",
        metadata={"to": "0xrouter", "recipient": "0x1111111111111111111111111111111111111111"},
    )
    sim = pack.simulate(plan)
    assert sim.ok is False
    assert sim.error_code == "revert"
    assert "bad_swap" in sim.error_message


def test_registry_supports_multiple_univ2_instances(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sepolia.yaml").write_text(
        json.dumps(
            {
                "enabled_dex_packs": ["univ2_sushi", "univ2_pancake"],
                "dex_packs": {
                    "univ2_sushi": {"type": "evm_univ2", "factory": "0xf1", "router": "0xr1", "rpc_http": "http://rpc"},
                    "univ2_pancake": {
                        "type": "evm_univ2",
                        "factory": "0xf2",
                        "router": "0xr2",
                        "rpc_http": "http://rpc",
                        "fee_bps": 25,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    reg = DEXPackRegistry(operator_state_path=str(tmp_path / "op_state.json"))
    reg.reload(family="evm", chain="sepolia", network="testnet")
    assert reg.enabled_names() == ["univ2_pancake", "univ2_sushi"]
    assert reg.get("univ2_sushi") is not None
    assert reg.get("univ2_pancake") is not None

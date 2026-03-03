from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.dex_packs.evm_univ2 import EVMUniV2Pack, calc_amount_out, parse_revert_reason
from bot.core.types_dex import TradeIntent, TxPlan


class _Fn:
    def __init__(self, ret):
        self._ret = ret

    def call(self):
        return self._ret


class _FactoryFunctions:
    def getPair(self, _a, _b):
        return _Fn("0x00000000000000000000000000000000000000aa")


class _PairFunctions:
    def getReserves(self):
        return _Fn((1_000_000, 2_000_000, 0))

    def token0(self):
        return _Fn("0x0000000000000000000000000000000000000001")

    def token1(self):
        return _Fn("0x0000000000000000000000000000000000000002")


class _RouterContract:
    def __init__(self):
        self.last = None

    def encodeABI(self, fn_name, args):
        self.last = (fn_name, args)
        return "0xdeadbeef"


@pytest.fixture
def intent() -> TradeIntent:
    return TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x0000000000000000000000000000000000000001",
        token_out="0x0000000000000000000000000000000000000002",
        amount_in=100_000,
        slippage_bps=100,
        ttl_s=30,
        strategy="test",
        dex_preference=None,
    )


def test_quote_uses_reserves_and_instance_name(monkeypatch, intent):
    pack = EVMUniV2Pack(
        config={
            "factory": "0x00000000000000000000000000000000000000f0",
            "router": "0x00000000000000000000000000000000000000f1",
            "fee_bps": 30,
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ2_sushi",
    )

    monkeypatch.setattr(pack, "_factory_contract", lambda: SimpleNamespace(functions=_FactoryFunctions()))
    monkeypatch.setattr(pack, "_pair_contract", lambda _addr: SimpleNamespace(functions=_PairFunctions()))

    seen = {}

    def _rec(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("adapters.dex_packs.evm_univ2.ops_metrics.record_dex_quote", _rec)

    q = pack.quote(intent)
    expected = calc_amount_out(intent.amount_in, 1_000_000, 2_000_000, 30)

    assert q.expected_out == expected
    assert q.dex == "univ2_sushi"
    assert q.route_summary.endswith(intent.token_out)
    assert seen.get("dex") == "univ2_sushi"


def test_build_swap_exact_tokens_for_tokens(monkeypatch, intent):
    pack = EVMUniV2Pack(
        config={
            "factory": "0x00000000000000000000000000000000000000f0",
            "router": "0x00000000000000000000000000000000000000f1",
            "fee_bps": 30,
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ2_pancake",
    )
    rc = _RouterContract()
    monkeypatch.setattr(pack, "_router_contract", lambda: rc)

    q = pack.quote(intent) if False else SimpleNamespace(min_out=12345, route_summary=f"{intent.token_in}->{intent.token_out}")
    plan = pack.build(intent, q)

    assert plan.raw_tx == "0xdeadbeef"
    assert plan.dex == "univ2_pancake"
    assert rc.last is not None
    assert rc.last[0] == "swapExactTokensForTokens"
    assert int(rc.last[1][0]) == intent.amount_in
    assert int(rc.last[1][1]) == 12345


def test_simulate_success_and_failure_bucketing(monkeypatch):
    pack = EVMUniV2Pack(
        config={
            "factory": "0x00000000000000000000000000000000000000f0",
            "router": "0x00000000000000000000000000000000000000f1",
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ2_sushi",
    )

    plan = TxPlan(
        family="evm",
        chain="sepolia",
        dex="univ2_sushi",
        value=0,
        raw_tx="0xdeadbeef",
        metadata={"to": "0x00000000000000000000000000000000000000f1", "recipient": "0x0000000000000000000000000000000000000abc"},
    )

    # success
    pack.w3.eth = SimpleNamespace(call=lambda _c: b"", estimate_gas=lambda _c: 210000)
    ok = pack.simulate(plan)
    assert ok.ok is True
    assert ok.gas_estimate == 210000

    # failure + reason bucket
    captured = {}

    def _sim_fail(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("adapters.dex_packs.evm_univ2.ops_metrics.record_dex_sim_fail", _sim_fail)

    def _raise(_c):
        raise Exception("execution reverted: INSUFFICIENT_OUTPUT_AMOUNT")

    pack.w3.eth = SimpleNamespace(call=_raise, estimate_gas=lambda _c: 0)
    bad = pack.simulate(plan)
    assert bad.ok is False
    assert bad.error_code == "insufficient_output"
    assert captured.get("reason") == "insufficient_output"


def test_parse_revert_reason_buckets_known_reverts():
    c1, _ = parse_revert_reason(Exception("execution reverted: INSUFFICIENT_OUTPUT_AMOUNT"))
    c2, _ = parse_revert_reason(Exception("execution reverted: INSUFFICIENT_LIQUIDITY"))
    assert c1 == "insufficient_output"
    assert c2 == "insufficient_liquidity"


def test_multiple_instances_keep_distinct_metric_label_names():
    a = EVMUniV2Pack(config={"factory": "0x1", "router": "0x2"}, instance_name="univ2_sushi")
    b = EVMUniV2Pack(config={"factory": "0x1", "router": "0x2"}, instance_name="univ2_pancake")
    assert a.name() == "univ2_sushi"
    assert b.name() == "univ2_pancake"

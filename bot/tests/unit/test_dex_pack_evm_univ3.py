from __future__ import annotations

from types import SimpleNamespace

from adapters.dex_packs.evm_univ3 import EVMUniV3Pack, _fee_tiers_from_cfg
from bot.core.types_dex import TradeIntent, TxPlan


class _Fn:
    def __init__(self, ret):
        self._ret = ret

    def call(self):
        return self._ret


class _QuoterFunctionsSingle:
    def __init__(self, by_fee: dict[int, int]):
        self.by_fee = by_fee

    def quoteExactInputSingle(self, _token_in, _token_out, fee, _amount_in, _sqrt):
        if fee not in self.by_fee:
            raise RuntimeError("unsupported fee")
        return _Fn(self.by_fee[fee])


class _QuoterFunctionsMulti:
    def __init__(self, out_amount: int):
        self.out_amount = out_amount
        self.last_path = None

    def quoteExactInput(self, path, _amount_in):
        self.last_path = path
        return _Fn(self.out_amount)


class _RouterContract:
    def __init__(self):
        self.last = None

    def encodeABI(self, fn_name, args):
        self.last = (fn_name, args)
        return "0xfeedbeef"


def _intent() -> TradeIntent:
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


def test_fee_tiers_safe_defaults_and_filtering():
    assert _fee_tiers_from_cfg({}) == [500, 3000, 10000]
    assert _fee_tiers_from_cfg({"fee_tiers": "100,3000,0,-1,3000"}) == [100, 3000]
    assert _fee_tiers_from_cfg({"fee_tiers": []}) == [500, 3000, 10000]


def test_quote_single_hop_selects_best_fee_tier(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x00000000000000000000000000000000000000f2",
            "swap_router": "0x00000000000000000000000000000000000000f3",
            "factory": "0x00000000000000000000000000000000000000f4",
            "fee_tiers": [500, 3000, 10000],
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ3_uni",
    )

    qf = _QuoterFunctionsSingle({500: 90_000, 3000: 120_000, 10000: 80_000})
    monkeypatch.setattr(pack, "_quoter_contract", lambda: SimpleNamespace(functions=qf))

    q = pack.quote(_intent())
    assert q.expected_out == 120_000
    assert q.dex == "univ3_uni"
    assert q.route_summary.endswith("@3000")


def test_quote_multi_hop_uses_quote_exact_input(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x00000000000000000000000000000000000000f2",
            "swap_router": "0x00000000000000000000000000000000000000f3",
            "factory": "0x00000000000000000000000000000000000000f4",
            "paths": {
                "0x0000000000000000000000000000000000000001->0x0000000000000000000000000000000000000002": {
                    "tokens": [
                        "0x0000000000000000000000000000000000000001",
                        "0x0000000000000000000000000000000000000003",
                        "0x0000000000000000000000000000000000000002",
                    ],
                    "fees": [500, 3000],
                }
            },
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ3_uni",
    )

    qf = _QuoterFunctionsMulti(out_amount=111_111)
    monkeypatch.setattr(pack, "_quoter_contract", lambda: SimpleNamespace(functions=qf))

    q = pack.quote(_intent())
    assert q.expected_out == 111_111
    assert "@500" in q.route_summary
    assert qf.last_path is not None


def test_build_uses_exact_input_single_or_exact_input(monkeypatch):
    base_cfg = {
        "quoter": "0x00000000000000000000000000000000000000f2",
        "swap_router": "0x00000000000000000000000000000000000000f3",
        "factory": "0x00000000000000000000000000000000000000f4",
        "recipient": "0x0000000000000000000000000000000000000abc",
    }
    intent = _intent()

    # single-hop
    pack_single = EVMUniV3Pack(config=base_cfg, instance_name="univ3_uni")
    rc1 = _RouterContract()
    monkeypatch.setattr(pack_single, "_router_contract", lambda: rc1)
    q1 = SimpleNamespace(min_out=1000, route_summary=f"{intent.token_in}->{intent.token_out}@3000")
    plan1 = pack_single.build(intent, q1)
    assert plan1.raw_tx == "0xfeedbeef"
    assert rc1.last[0] == "exactInputSingle"

    # multi-hop
    cfg_multi = {
        **base_cfg,
        "paths": {
            f"{intent.token_in.lower()}->{intent.token_out.lower()}": {
                "tokens": [intent.token_in, "0x0000000000000000000000000000000000000003", intent.token_out],
                "fees": [500, 3000],
            }
        },
    }
    pack_multi = EVMUniV3Pack(config=cfg_multi, instance_name="univ3_uni")
    rc2 = _RouterContract()
    monkeypatch.setattr(pack_multi, "_router_contract", lambda: rc2)
    q2 = SimpleNamespace(min_out=1000, route_summary="dummy")
    plan2 = pack_multi.build(intent, q2)
    assert plan2.raw_tx == "0xfeedbeef"
    assert rc2.last[0] == "exactInput"


def test_simulate_eth_call_success_and_revert_bucket(monkeypatch):
    pack = EVMUniV3Pack(
        config={
            "quoter": "0x00000000000000000000000000000000000000f2",
            "swap_router": "0x00000000000000000000000000000000000000f3",
            "factory": "0x00000000000000000000000000000000000000f4",
            "recipient": "0x0000000000000000000000000000000000000abc",
        },
        instance_name="univ3_uni",
    )

    plan = TxPlan(
        family="evm",
        chain="sepolia",
        dex="univ3_uni",
        value=0,
        raw_tx="0xfeedbeef",
        metadata={"to": "0x00000000000000000000000000000000000000f3", "recipient": "0x0000000000000000000000000000000000000abc"},
    )

    pack.w3.eth = SimpleNamespace(call=lambda _c: b"", estimate_gas=lambda _c: 190000)
    ok = pack.simulate(plan)
    assert ok.ok is True
    assert ok.gas_estimate == 190000

    def _raise(_c):
        raise Exception("execution reverted: INSUFFICIENT_LIQUIDITY")

    pack.w3.eth = SimpleNamespace(call=_raise, estimate_gas=lambda _c: 0)
    bad = pack.simulate(plan)
    assert bad.ok is False
    assert bad.error_code == "insufficient_liquidity"

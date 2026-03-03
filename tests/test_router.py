from __future__ import annotations

import time

from adapters.dex_packs.base import DEXPack
from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.router import TradeRouter
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan


class _FakePack(DEXPack):
    def __init__(self, *, name: str, out: int = 0, delay_s: float = 0.0, fail: bool = False):
        super().__init__(config={}, instance_name=name)
        self._name = name
        self._out = out
        self._delay = delay_s
        self._fail = fail

    def name(self) -> str:
        return self._instance_name or self._name

    def family_supported(self) -> str:
        return "evm"

    def quote(self, intent: TradeIntent) -> Quote:
        if self._delay > 0:
            time.sleep(self._delay)
        if self._fail:
            raise ValueError("quote_failed")
        return Quote(
            dex=self.name(),
            expected_out=self._out,
            min_out=max(1, int(self._out * 0.99)),
            price_impact_bps=1.0,
            fee_estimate=0.0,
            route_summary=f"{intent.token_in}->{intent.token_out}",
            quote_latency_ms=1.0,
        )

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        return TxPlan(family=intent.family, chain=intent.chain, dex=self.name(), raw_tx="0x", value=0)

    def simulate(self, plan: TxPlan) -> SimResult:
        return SimResult(ok=True, gas_estimate=1)


def _intent(preference: str | None = None) -> TradeIntent:
    return TradeIntent(
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="0x00000000000000000000000000000000000000a1",
        token_out="0x00000000000000000000000000000000000000b1",
        amount_in=1_000,
        slippage_bps=100,
        ttl_s=30,
        strategy="default",
        dex_preference=preference,
    )


def test_router_selects_best_expected_out():
    reg = DEXPackRegistry(operator_state_path="ops/operator_state.json")
    reg._packs = {
        "dex_a": _FakePack(name="dex_a", out=1000),
        "dex_b": _FakePack(name="dex_b", out=1200),
        "dex_c": _FakePack(name="dex_c", out=900),
    }
    reg._enabled = set(reg._packs.keys())
    router = TradeRouter(registry=reg, quote_timeout_ms=500, max_workers=3)
    sel = router.route(_intent())
    assert sel is not None
    assert sel.dex == "dex_b"
    assert sel.quote.expected_out == 1200
    assert len(sel.quote_table) == 3
    assert [x.dex for x in sel.quote_table] == ["dex_b", "dex_a", "dex_c"]


def test_router_honors_dex_preference():
    reg = DEXPackRegistry(operator_state_path="ops/operator_state.json")
    reg._packs = {
        "dex_a": _FakePack(name="dex_a", out=1000),
        "dex_b": _FakePack(name="dex_b", out=1200),
    }
    reg._enabled = set(reg._packs.keys())
    router = TradeRouter(registry=reg, quote_timeout_ms=500, max_workers=2)
    sel = router.route(_intent(preference="dex_a"))
    assert sel is not None
    assert sel.dex == "dex_a"
    assert len(sel.quote_table) == 1


def test_router_handles_fail_and_timeout():
    reg = DEXPackRegistry(operator_state_path="ops/operator_state.json")
    reg._packs = {
        "dex_ok": _FakePack(name="dex_ok", out=1000),
        "dex_fail": _FakePack(name="dex_fail", fail=True),
        "dex_slow": _FakePack(name="dex_slow", out=2000, delay_s=0.25),
    }
    reg._enabled = set(reg._packs.keys())
    router = TradeRouter(registry=reg, quote_timeout_ms=100, max_workers=3)
    scan = router.arb_scan(_intent())
    by_name = {r.dex: r for r in scan}
    assert by_name["dex_ok"].ok is True
    assert by_name["dex_fail"].ok is False
    assert by_name["dex_slow"].ok is False
    sel = router.route(_intent())
    assert sel is not None
    assert sel.dex == "dex_ok"


def test_router_selection_is_deterministic_on_tie():
    reg = DEXPackRegistry(operator_state_path="ops/operator_state.json")
    reg._packs = {
        "dex_b": _FakePack(name="dex_b", out=1000),
        "dex_a": _FakePack(name="dex_a", out=1000),
    }
    reg._enabled = set(reg._packs.keys())
    router = TradeRouter(registry=reg, quote_timeout_ms=500, max_workers=2)
    sel = router.route(_intent())
    assert sel is not None
    assert [x.dex for x in sel.quote_table] == ["dex_b", "dex_a"]
    assert sel.dex == "dex_b"

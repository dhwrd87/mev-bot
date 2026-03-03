from __future__ import annotations

import json
import time
from dataclasses import dataclass

from bot.core.opportunity_engine.types import Opportunity
from bot.core.types_dex import Quote, SimResult, TxPlan
from core.orchestrator import OpportunityOrchestrator
from core.sizing import CostModel, search_best_size


@dataclass
class _Sel:
    dex: str
    quote: Quote
    quote_table: list

    @property
    def candidates(self):
        return self.quote_table


class _FakeRouter:
    def __init__(self, edge_by_size: dict[int, int]):
        self.edge_by_size = dict(edge_by_size)

    def route(self, intent):
        out = self.edge_by_size.get(int(intent.amount_in), 0)
        if out <= 0:
            return None
        q = Quote(
            dex="univ3",
            expected_out=out,
            min_out=max(1, out - 1),
            price_impact_bps=1.0,
            fee_estimate=1.0,
            route_summary="a->b",
            quote_latency_ms=1.0,
        )
        return _Sel(dex="univ3", quote=q, quote_table=[])


class _FakePack:
    def build(self, intent, quote):
        return TxPlan(family=intent.family, chain=intent.chain, dex=quote.dex, raw_tx="0x1", value=0)

    def simulate(self, _plan):
        return SimResult(ok=True, gas_estimate=100000)


class _FakeRegistry:
    def get(self, _dex):
        return _FakePack()


def _opp(oid: str, ts: float, confidence: float, edge_bps: float, size_candidates: list[int]) -> Opportunity:
    return Opportunity(
        id=oid,
        ts=ts,
        family="evm",
        chain="sepolia",
        network="testnet",
        type="cross_dex_arb",
        size_candidates=size_candidates,
        expected_edge_bps=edge_bps,
        confidence=confidence,
        required_capabilities=["quote", "build", "simulate"],
        constraints={
            "token_in": "0x0000000000000000000000000000000000000001",
            "token_out": "0x0000000000000000000000000000000000000002",
            "profit_est_usd": 50.0,
        },
        refs={},
    )


def test_priority_formula_prefers_higher_profit_confidence_freshness(tmp_path, monkeypatch):
    state = tmp_path / "operator_state.json"
    state.write_text(json.dumps({"state": "TRADING", "mode": "dryrun", "kill_switch": False}), encoding="utf-8")
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state))

    orch = OpportunityOrchestrator(router=_FakeRouter({100: 110}), registry=_FakeRegistry())
    now = time.time()
    strong = _opp("strong", now, 0.9, 20.0, [100, 200])
    weak = _opp("weak", now - 15.0, 0.3, 20.0, [100, 200])

    p_strong = orch._priority(strong, now=now)
    p_weak = orch._priority(weak, now=now)
    assert p_strong > p_weak

    orch.enqueue(weak)
    orch.enqueue(strong)
    first = orch._pop()
    assert first is not None
    assert first.opportunity.id == "strong"


def test_sizing_search_coarse_and_refine_prefers_best_net():
    # Parabola centered near 2500 gives deterministic best-size behavior.
    def scorer(size: int) -> float:
        return -abs(float(size) - 2500.0)

    out = search_best_size([500, 1_000, 2_000, 4_000, 8_000], scorer, coarse_points=5, refine_points=5)
    assert out.best_size >= 2000
    assert out.best_size <= 3000
    assert len(out.evaluated) >= 5


def test_cost_model_includes_gas_and_flashloan_fees():
    cm = CostModel(gas_cost_usd=5.0, fee_bps=5.0, flashloan_fee_bps=9.0)
    net = cm.net_profit_usd(amount_in=10_000, edge_bps=30.0, quote_fee_usd=1.0)
    # Gross=30, variable=(14), fixed=6 => 10
    assert round(net, 6) == 10.0

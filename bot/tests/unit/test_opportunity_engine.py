import json
import time
from dataclasses import dataclass

import pytest

from bot.core.opportunity_engine.types import Opportunity
from bot.core.types_dex import Quote, SimResult, TxPlan
from bot.orchestrator.opportunity_orchestrator import OpportunityOrchestrator


@dataclass
class _Sel:
    dex: str
    quote: Quote
    candidates: list


class _FakeRouter:
    def __init__(self, quote: Quote):
        self._quote = quote

    def route(self, _intent):
        return _Sel(dex=self._quote.dex, quote=self._quote, candidates=[])


class _FakePack:
    def __init__(self, sim_ok: bool = True):
        self.sim_ok = sim_ok

    def build(self, intent, quote):
        return TxPlan(
            family=intent.family,
            chain=intent.chain,
            dex=quote.dex,
            raw_tx="0x01",
            value=0,
            metadata={"to": "0xabc"},
        )

    def simulate(self, _plan):
        if self.sim_ok:
            return SimResult(ok=True, gas_estimate=100000)
        return SimResult(ok=False, error_code="sim_failed", error_message="failed")


class _FakeRegistry:
    def __init__(self, pack):
        self.pack = pack

    def get(self, _name):
        return self.pack


def _opp(*, ts: float, edge_bps: float, oid: str = "o1") -> Opportunity:
    return Opportunity(
        id=oid,
        ts=ts,
        family="evm",
        chain="sepolia",
        network="testnet",
        type="cross_dex_arb",
        size_candidates=[1000, 2000],
        expected_edge_bps=edge_bps,
        confidence=0.8,
        required_capabilities=["quote", "build", "simulate"],
        constraints={"token_in": "0xTokenA", "token_out": "0xTokenB", "ttl_s": 30, "slippage_bps": 50},
        refs={"source": "test"},
    )


def _write_state(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_queue_ordering_prefers_fresher_opportunity(monkeypatch, tmp_path):
    state_path = tmp_path / "operator_state.json"
    _write_state(state_path, {"state": "TRADING", "mode": "dryrun", "kill_switch": False})
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))

    q = Quote(dex="univ3", expected_out=1200, min_out=1100, price_impact_bps=1.0, fee_estimate=1.0, route_summary="a->b", quote_latency_ms=1.0)
    orch = OpportunityOrchestrator(router=_FakeRouter(q), registry=_FakeRegistry(_FakePack()), detectors=[])

    now = time.time()
    old = _opp(ts=now - 20, edge_bps=10.0, oid="old")
    fresh = _opp(ts=now, edge_bps=10.0, oid="fresh")

    orch.enqueue(old)
    orch.enqueue(fresh)
    first = orch.pop_next()
    assert first is not None
    assert first.id == "fresh"


def test_rejects_when_operator_paused(monkeypatch, tmp_path):
    state_path = tmp_path / "operator_state.json"
    _write_state(state_path, {"state": "PAUSED", "mode": "dryrun", "kill_switch": False})
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))

    q = Quote(dex="univ3", expected_out=1200, min_out=1100, price_impact_bps=1.0, fee_estimate=1.0, route_summary="a->b", quote_latency_ms=1.0)
    orch = OpportunityOrchestrator(router=_FakeRouter(q), registry=_FakeRegistry(_FakePack()), detectors=[])
    decision = orch.process_opportunity(_opp(ts=time.time(), edge_bps=20.0))
    assert decision.status == "rejected"
    assert decision.reason == "operator_not_trading"


def test_rejects_on_edge_threshold(monkeypatch, tmp_path):
    state_path = tmp_path / "operator_state.json"
    _write_state(state_path, {"state": "TRADING", "mode": "dryrun", "kill_switch": False})
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))
    monkeypatch.setenv("MIN_EDGE_BPS", "50")

    q = Quote(dex="univ3", expected_out=1200, min_out=1100, price_impact_bps=1.0, fee_estimate=1.0, route_summary="a->b", quote_latency_ms=1.0)
    orch = OpportunityOrchestrator(router=_FakeRouter(q), registry=_FakeRegistry(_FakePack()), detectors=[])
    decision = orch.process_opportunity(_opp(ts=time.time(), edge_bps=5.0))
    assert decision.status == "rejected"
    assert decision.reason == "edge_below_threshold"


def test_live_mode_requires_sim_success(monkeypatch, tmp_path):
    state_path = tmp_path / "operator_state.json"
    _write_state(state_path, {"state": "TRADING", "mode": "live", "kill_switch": False})
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))

    q = Quote(dex="univ3", expected_out=1500, min_out=1400, price_impact_bps=1.0, fee_estimate=1.0, route_summary="a->b", quote_latency_ms=1.0)
    orch = OpportunityOrchestrator(router=_FakeRouter(q), registry=_FakeRegistry(_FakePack(sim_ok=False)), detectors=[])
    decision = orch.process_opportunity(_opp(ts=time.time(), edge_bps=100.0))
    assert decision.status == "rejected"
    assert decision.reason == "sim_failed"

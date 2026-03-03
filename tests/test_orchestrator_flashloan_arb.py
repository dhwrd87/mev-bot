from __future__ import annotations

import json
import time
from dataclasses import dataclass

from adapters.flashloans.base import FlashloanProvider
from bot.core.opportunity_engine.types import Opportunity
from bot.core.types_dex import Quote, SimResult, TxPlan
from core.orchestrator import OpportunityOrchestrator
from strategies.flashloan_arb import FlashloanArbStrategy


@dataclass
class _Sel:
    dex: str
    quote: Quote
    quote_table: list

    @property
    def candidates(self):
        return self.quote_table


class _Router:
    def route(self, intent):
        q = Quote(
            dex="dex_a",
            expected_out=int(intent.amount_in * 1.1),
            min_out=max(1, int(intent.amount_in * 1.09)),
            price_impact_bps=1.0,
            fee_estimate=0.0,
            route_summary="a->b",
            quote_latency_ms=1.0,
        )
        return _Sel(dex="dex_a", quote=q, quote_table=[])


class _Pack:
    def __init__(self, ok: bool):
        self.ok = ok

    def build(self, intent, quote):
        return TxPlan(family=intent.family, chain=intent.chain, dex=quote.dex, value=0, raw_tx="0x01")

    def simulate(self, plan):
        if self.ok:
            return SimResult(ok=True, gas_estimate=100000)
        return SimResult(ok=False, error_code="revert", error_message="flashloan repay failed", logs=["repay"])


class _Registry:
    def __init__(self, pack):
        self._pack = pack

    def get(self, _name):
        return self._pack


class _Provider(FlashloanProvider):
    def supported_assets(self):
        return ["A"]

    def fee_bps(self) -> float:
        return 9.0

    def build_flashloan_wrapper(self, plan: TxPlan) -> TxPlan:
        md = dict(plan.metadata or {})
        md["flashloan_wrapped"] = True
        return TxPlan(
            family=plan.family,
            chain=plan.chain,
            dex=plan.dex,
            value=plan.value,
            metadata=md,
            raw_tx=plan.raw_tx,
            instruction_bundle=plan.instruction_bundle,
        )

    def name(self) -> str:
        return "aave_v3"


def _opp(kind: str = "xarb") -> Opportunity:
    return Opportunity(
        id="o1",
        ts=time.time(),
        family="evm",
        chain="sepolia",
        network="testnet",
        type=kind,
        size_candidates=[1000],
        expected_edge_bps=50.0,
        confidence=0.9,
        required_capabilities=["quote", "build", "simulate"],
        constraints={"token_in": "A", "token_out": "B", "best_dex": "dex_a", "sell_dex": "dex_b"},
        refs={},
    )


def test_orchestrator_flashloan_arb_sim_failure_bucket(monkeypatch, tmp_path):
    st = tmp_path / "operator_state.json"
    st.write_text(json.dumps({"state": "TRADING", "mode": "paper", "kill_switch": False}), encoding="utf-8")
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(st))
    monkeypatch.setenv("INVENTORY_USD", "0")
    monkeypatch.setenv("FLASHLOAN_MIN_SIZE_USD", "1")

    provider = _Provider()
    orch = OpportunityOrchestrator(
        router=_Router(),
        registry=_Registry(_Pack(ok=False)),
        flashloan_provider=provider,
        flashloan_arb_strategy=FlashloanArbStrategy(provider),
    )
    decision = orch.process(_opp("xarb"))
    assert decision.status == "rejected"
    assert decision.reason == "sim_repay_failed"

from __future__ import annotations

from adapters.flashloans.base import FlashloanProvider
from bot.core.opportunity_engine.types import Opportunity
from bot.core.types_dex import TxPlan
from strategies.flashloan_arb import FlashloanArbStrategy


class _Provider(FlashloanProvider):
    def supported_assets(self):
        return ["0x1"]

    def fee_bps(self) -> float:
        return 9.0

    def build_flashloan_wrapper(self, plan: TxPlan) -> TxPlan:
        md = dict(plan.metadata or {})
        md["wrapped"] = True
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


def _opp(kind: str) -> Opportunity:
    base = {
        "id": "o1",
        "ts": 1.0,
        "family": "evm",
        "chain": "sepolia",
        "network": "testnet",
        "type": kind,
        "size_candidates": [1000],
        "expected_edge_bps": 50.0,
        "confidence": 0.8,
        "required_capabilities": ["quote", "build", "simulate"],
        "constraints": {
            "token_in": "A",
            "token_out": "B",
            "best_dex": "dex_a",
            "sell_dex": "dex_b",
            "path_tokens": ["A", "B", "C", "A"],
            "path_dexes": ["dex_a", "dex_b", "dex_c"],
        },
        "refs": {},
    }
    return Opportunity(**base)


def test_flashloan_arb_builds_xarb_plan():
    s = FlashloanArbStrategy(_Provider())
    p = s.build_plan(opportunity=_opp("xarb"), size=1000, expected_profit_after_costs=12.3)
    assert p.kind == "xarb"
    assert p.path_tokens == ["A", "B", "A"]
    assert p.path_dexes == ["dex_a", "dex_b"]
    assert p.repay_amount == 1001


def test_flashloan_arb_builds_triarb_plan_and_attach():
    s = FlashloanArbStrategy(_Provider())
    ap = s.build_plan(opportunity=_opp("triarb"), size=2000, expected_profit_after_costs=22.0)
    tx = TxPlan(family="evm", chain="sepolia", dex="dex_a", value=0, raw_tx="0xabc")
    wrapped = s.attach_to_txplan(tx, ap)
    assert wrapped.metadata["flashloan_arb"]["kind"] == "triarb"
    assert wrapped.instruction_bundle["flashloan_arb_bundle"]["repay"]["amount"] == ap.repay_amount


def test_flashloan_arb_sim_failure_bucketing():
    s = FlashloanArbStrategy(_Provider())
    assert s.bucket_sim_failure("revert", "flashloan repay failed", ["repay"]) == "sim_repay_failed"
    assert s.bucket_sim_failure("revert", "insufficient liquidity", []) == "sim_insufficient_liquidity"

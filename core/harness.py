from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.core.opportunity_engine.types import Opportunity
from bot.core.router import TradeRouter
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan
from core.orchestrator import OpportunityOrchestrator
from ops import metrics as ops_metrics
from ops.health_snapshot import HealthSnapshotWriter


def _parse_sim_pattern(value: str) -> List[bool]:
    toks = [t.strip().lower() for t in str(value or "").split(",") if t.strip()]
    if not toks:
        return [True]
    out: List[bool] = []
    for tok in toks:
        out.append(tok in {"1", "ok", "true", "pass", "success"})
    return out or [True]


def _write_operator_state(path: str, *, state: str = "TRADING", mode: str = "paper") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": str(state).upper(),
        "mode": str(mode).lower(),
        "kill_switch": False,
        "last_actor": "harness",
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    p.write_text(json.dumps(payload) + "\n", encoding="utf-8")


@dataclass
class SyntheticOpportunitySource:
    family: str
    chain: str
    network: str
    token_in: str
    token_out: str
    base_sizes: List[int]
    xarb_edge_bps: float
    triarb_edge_bps: float
    confidence: float = 0.85
    _seq: int = 0

    def emit(self, now: Optional[float] = None) -> List[Opportunity]:
        t = float(time.time() if now is None else now)
        self._seq += 1
        base_refs = {"source": "synthetic_harness", "seq": str(self._seq), "profit_est_usd": "12.5"}
        common = {
            "ts": t,
            "family": self.family,
            "chain": self.chain,
            "network": self.network,
            "size_candidates": list(self.base_sizes),
            "confidence": float(self.confidence),
            "required_capabilities": ["quote", "build", "simulate"],
            "constraints": {
                "token_in": self.token_in,
                "token_out": self.token_out,
                "slippage_bps": 50,
                "ttl_s": 30,
            },
        }
        return [
            Opportunity(
                id=f"xarb-{self._seq}",
                type="xarb",
                expected_edge_bps=float(self.xarb_edge_bps),
                refs=dict(base_refs, pattern="xarb"),
                **common,
            ),
            Opportunity(
                id=f"triarb-{self._seq}",
                type="triarb",
                expected_edge_bps=float(self.triarb_edge_bps),
                refs=dict(base_refs, pattern="triarb"),
                **common,
            ),
        ]


class _SyntheticDexPack:
    def __init__(self, *, name: str, edge_bps: float, sim_pattern: List[bool]) -> None:
        self._name = str(name)
        self._edge_bps = float(edge_bps)
        self._sim_pattern = list(sim_pattern) or [True]
        self._sim_idx = 0

    def quote(self, intent: TradeIntent) -> Quote:
        amt = max(1, int(intent.amount_in))
        bump = max(1, int(amt * max(0.0, self._edge_bps) / 10_000.0))
        expected_out = amt + bump
        min_out = max(1, int(expected_out * (1.0 - (float(intent.slippage_bps) / 10_000.0))))
        return Quote(
            dex=self._name,
            expected_out=expected_out,
            min_out=min_out,
            price_impact_bps=2.0,
            fee_estimate=0.02,
            route_summary=f"{self._name}:synthetic",
            quote_latency_ms=2.0,
        )

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        return TxPlan(
            family=intent.family,
            chain=intent.chain,
            dex=quote.dex,
            value=0,
            raw_tx=f"0xsynth-{quote.dex}-{intent.amount_in}",
            metadata={"quote_expected_out": quote.expected_out},
        )

    def simulate(self, _plan: TxPlan) -> SimResult:
        ok = bool(self._sim_pattern[self._sim_idx % len(self._sim_pattern)])
        self._sim_idx += 1
        if ok:
            return SimResult(ok=True, gas_estimate=100_000, logs=["ok"])
        return SimResult(ok=False, error_code="sim_revert", error_message="synthetic_fail", logs=["revert"])


class _SyntheticRegistry:
    def __init__(self, packs: Dict[str, _SyntheticDexPack]) -> None:
        self._packs = dict(packs)

    def enabled_names(self) -> set[str]:
        return set(self._packs.keys())

    def get(self, name: str) -> Optional[_SyntheticDexPack]:
        return self._packs.get(str(name))


def run_paper_harness(
    *,
    duration_s: float,
    tick_s: float = 0.25,
    operator_state_path: Optional[str] = None,
    snapshot_path: Optional[str] = None,
    sim_pattern: str = "ok,ok,ok,fail",
) -> Dict[str, Any]:
    family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
    chain = str(os.getenv("CHAIN", "sepolia")).strip().lower() or "sepolia"
    network = str(os.getenv("CHAIN_NETWORK", "testnet")).strip().lower() or "testnet"
    op_state_path = str(
        operator_state_path
        or os.getenv("OPERATOR_STATE_PATH")
        or (Path("runtime") / "harness_operator_state.json")
    )
    snap_path = str(snapshot_path or os.getenv("HEALTH_SNAPSHOT_PATH", "ops/health_snapshot.json"))
    _write_operator_state(op_state_path, state="TRADING", mode="paper")

    sim = _parse_sim_pattern(sim_pattern)
    packs = {
        "synthetic_xarb": _SyntheticDexPack(name="synthetic_xarb", edge_bps=35.0, sim_pattern=sim),
        "synthetic_triarb": _SyntheticDexPack(name="synthetic_triarb", edge_bps=28.0, sim_pattern=sim),
    }
    registry = _SyntheticRegistry(packs)
    router = TradeRouter(registry=registry, quote_timeout_ms=150, max_workers=4)
    orchestrator = OpportunityOrchestrator(
        router=router,
        registry=registry,
        strategy="harness",
        operator_state_path=op_state_path,
    )
    source = SyntheticOpportunitySource(
        family=family,
        chain=chain,
        network=network,
        token_in="0x0000000000000000000000000000000000000001",
        token_out="0x0000000000000000000000000000000000000002",
        base_sizes=[1_000, 2_500, 5_000],
        xarb_edge_bps=35.0,
        triarb_edge_bps=22.0,
    )
    snapshot = HealthSnapshotWriter(path=snap_path, interval_s=max(1.0, tick_s), window_s=600.0)

    pnl_total = 0.0
    fees_total = 0.0
    peak = 0.0
    drawdown = 0.0
    processed = 0
    rejects: Dict[str, int] = {}
    t_end = time.monotonic() + max(0.5, float(duration_s))

    while time.monotonic() < t_end:
        for opp in source.emit():
            orchestrator.enqueue(opp)
        while True:
            decision = orchestrator.process_next()
            if decision.status == "empty":
                break
            processed += 1
            if decision.status == "rejected":
                rejects[decision.reason] = rejects.get(decision.reason, 0) + 1
            if decision.plan is not None and decision.reason == "ok":
                pnl_total += float(decision.plan.expected_profit_after_costs)
                fees_total += 0.01
                peak = max(peak, pnl_total)
                drawdown = max(drawdown, peak - pnl_total)
                ops_metrics.set_pnl_realized(family=family, chain=chain, strategy="harness", usd=pnl_total)
                ops_metrics.set_fees_total(family=family, chain=chain, strategy="harness", usd=fees_total)
                ops_metrics.set_drawdown(family=family, chain=chain, strategy="harness", usd=drawdown)
        snapshot.maybe_write(family=family, chain=chain, state="TRADING", mode="paper", now=time.time())
        time.sleep(max(0.0, float(tick_s)))

    snapshot.maybe_write(family=family, chain=chain, state="TRADING", mode="paper", force=True, now=time.time())
    snap_data = orchestrator.snapshot(now=time.time())
    return {
        "processed": int(processed),
        "pnl_total": float(pnl_total),
        "fees_total": float(fees_total),
        "drawdown": float(drawdown),
        "rejects": rejects,
        "orchestrator": snap_data,
        "snapshot_path": snap_path,
        "operator_state_path": op_state_path,
    }

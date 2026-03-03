from __future__ import annotations

import heapq
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.operator_control import get_operator_state
from bot.core.opportunity_engine.scoring import opportunity_score
from bot.core.opportunity_engine.types import (
    MarketEvent as EngineMarketEvent,
    Opportunity,
    TradeLeg,
    TradePlan,
)
from bot.core.router import TradeRouter
from bot.core.telemetry import orchestrator_decisions_total
from bot.core.types_dex import TradeIntent
from bot.detectors.base import BaseDetector
from bot.orchestration.orchestrator import OrchestratorConfig
from bot.sim.fork import ForkSimulator
from bot.strategy.dex_arb import DexArbStrategy
from bot.strategy.base import BaseStrategy
from ops import metrics as ops_metrics


@dataclass(frozen=True)
class OrchestratorDecision:
    status: str
    reason: str
    opportunity_id: str
    plan: Optional[TradePlan] = None


class UnifiedOrchestrator:
    def __init__(
        self,
        *,
        router: TradeRouter,
        registry: DEXPackRegistry,
        detectors: List[BaseDetector],
        strategy: str = "opportunity_engine",
        execute_cb: Optional[Callable[[TradePlan, Dict[str, Any]], bool]] = None,
        operator_state_path: Optional[str] = None,
        mode_cfg: Optional[OrchestratorConfig] = None,
        fork_simulator: Optional[ForkSimulator] = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.detectors = list(detectors)
        self.strategy = str(strategy or "opportunity_engine")
        self.execute_cb = execute_cb
        self.operator_state_path = operator_state_path
        self.mode_cfg = mode_cfg or OrchestratorConfig()
        self.fork_simulator = fork_simulator
        self.strategy_registry: dict[str, BaseStrategy] = {}

        self.min_edge_bps = float(os.getenv("MIN_EDGE_BPS", "2.0"))
        self.min_profit_after_cost = float(os.getenv("MIN_PROFIT_AFTER_COST", "0.01"))
        self.max_fee = float(os.getenv("MAX_FEE", "1000000000000"))
        self.max_daily_loss = float(os.getenv("MAX_DAILY_LOSS", "1000"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))
        self.gas_cost_estimate = float(os.getenv("OPP_GAS_COST_EST", "0.0"))

        self._pq: List[Tuple[float, float, Opportunity]] = []
        self._init_default_strategy_registry()

    def _init_default_strategy_registry(self) -> None:
        # Default strategy mapping for orchestrated opportunity types.
        try:
            self.strategy_registry["cross_dex_arb"] = DexArbStrategy(
                router=self.router,
                registry=self.registry,
                chain=os.getenv("CHAIN", "sepolia"),
            )
        except Exception:
            # Keep orchestrator startup resilient even if strategy deps are unavailable.
            pass

    def register_strategy(self, opportunity_type: str, strategy: BaseStrategy) -> None:
        key = str(opportunity_type or "").strip().lower()
        if not key:
            return
        self.strategy_registry[key] = strategy

    def _mode(self, op_state: Dict[str, Any]) -> str:
        return str(op_state.get("mode", "dryrun")).strip().lower() or "dryrun"

    def _is_strategy_enabled(self, op_state: Dict[str, Any]) -> bool:
        enabled = op_state.get("strategies_enabled")
        if isinstance(enabled, list) and enabled:
            return self.strategy in {str(x).strip().lower() for x in enabled}
        disabled = op_state.get("strategies_disabled")
        if isinstance(disabled, list) and disabled:
            return self.strategy not in {str(x).strip().lower() for x in disabled}
        return True

    def _risk_gate(self, opp: Opportunity) -> tuple[bool, str]:
        if float(opp.expected_edge_bps) < self.min_edge_bps:
            return False, "edge_below_threshold"
        if any(int(s) <= 0 for s in opp.size_candidates):
            return False, "invalid_size_candidates"
        return True, "ok"

    def _estimate_profit_after_costs(self, *, amount_in: int, edge_bps: float, fee_estimate: float) -> float:
        gross = float(amount_in) * max(0.0, float(edge_bps)) / 10_000.0
        costs = max(0.0, float(fee_estimate)) + max(0.0, self.gas_cost_estimate)
        return gross - costs

    def pick_mode(self, opp: Mapping[str, Any]) -> tuple[str, str]:
        if opp.get("type") == "stealth_hint":
            return "stealth", "hint"
        if float(opp.get("gas_gwei", 0.0) or 0.0) >= self.mode_cfg.gas_spike_gwei:
            return "stealth", "gas_spike"
        if int(opp.get("detected_snipers", 0) or 0) >= self.mode_cfg.min_snipers_active and bool(
            opp.get("vulnerable_flow")
        ):
            return "hunter", "snipers_active"
        return ("stealth", "exact_output") if bool(opp.get("exact_output")) else ("hunter", "default")

    def _normalize_event(self, event: Any) -> EngineMarketEvent:
        if isinstance(event, EngineMarketEvent):
            return event
        if isinstance(event, Mapping):
            payload = event.get("payload") or {}
            refs = event.get("refs") or {}
            tx_hash = event.get("tx_hash")
            if tx_hash and "tx_hash" not in refs:
                refs = dict(refs)
                refs["tx_hash"] = str(tx_hash)
            return EngineMarketEvent(
                id=str(event.get("id") or f"event:{int(time.time() * 1000)}"),
                ts=float(event.get("ts") or time.time()),
                family=str(event.get("family") or "evm"),
                chain=str(event.get("chain") or "unknown"),
                network=str(event.get("network") or "testnet"),
                token_in=str(event.get("token_in") or payload.get("token_in") or ""),
                token_out=str(event.get("token_out") or payload.get("token_out") or ""),
                amount_hint=int(event.get("amount_in") or payload.get("amount_in") or 0),
                dex_hint=(str(event.get("dex")) if event.get("dex") is not None else None),
                source=str(payload.get("source") or "mempool"),
                refs={str(k): str(v) for k, v in dict(refs).items()},
            )

        payload = getattr(event, "payload", {}) or {}
        refs = dict(getattr(event, "refs", {}) or {})
        tx_hash = getattr(event, "tx_hash", None)
        if tx_hash and "tx_hash" not in refs:
            refs["tx_hash"] = str(tx_hash)
        return EngineMarketEvent(
            id=str(getattr(event, "id", f"event:{int(time.time() * 1000)}")),
            ts=float(getattr(event, "ts", time.time())),
            family=str(getattr(event, "family", "evm")),
            chain=str(getattr(event, "chain", "unknown")),
            network=str(getattr(event, "network", "testnet")),
            token_in=str(getattr(event, "token_in", None) or payload.get("token_in") or ""),
            token_out=str(getattr(event, "token_out", None) or payload.get("token_out") or ""),
            amount_hint=int(getattr(event, "amount_in", None) or payload.get("amount_in") or 0),
            dex_hint=(str(getattr(event, "dex", None)) if getattr(event, "dex", None) is not None else None),
            source=str(payload.get("source") or "mempool"),
            refs={str(k): str(v) for k, v in refs.items()},
        )

    def on_event(self, event: Any) -> List[Opportunity]:
        normalized = self._normalize_event(event)
        out: List[Opportunity] = []
        for det in self.detectors:
            try:
                out.extend(det.on_event(normalized))
            except Exception as e:
                ops_metrics.record_opportunity_rejected(
                    family=normalized.family,
                    chain=normalized.chain,
                    strategy=self.strategy,
                    reason=f"detector_error:{det.name()}:{e}",
                )
        for opp in out:
            self.enqueue(opp)
        return out

    def enqueue(self, opp: Opportunity) -> None:
        score = opportunity_score(opp)
        heapq.heappush(self._pq, (-score, time.time(), opp))
        ops_metrics.record_opportunity_seen(
            family=opp.family,
            chain=opp.chain,
            dex=str(opp.constraints.get("best_dex") or "unknown"),
            strategy=self.strategy,
        )
        ops_metrics.set_opportunity_queue_depth(
            family=opp.family,
            chain=opp.chain,
            strategy=self.strategy,
            depth=len(self._pq),
        )

    def pop_next(self) -> Optional[Opportunity]:
        if not self._pq:
            return None
        _, _, opp = heapq.heappop(self._pq)
        ops_metrics.set_opportunity_queue_depth(
            family=opp.family,
            chain=opp.chain,
            strategy=self.strategy,
            depth=len(self._pq),
        )
        return opp

    def process_next(self) -> OrchestratorDecision:
        opp = self.pop_next()
        if opp is None:
            return OrchestratorDecision(status="empty", reason="queue_empty", opportunity_id="")
        return self.process_opportunity(opp)

    def process_opportunity(self, opp: Opportunity) -> OrchestratorDecision:
        op_state = get_operator_state(path=self.operator_state_path)
        mode = self._mode(op_state)

        if bool(op_state.get("kill_switch", False)):
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="operator_kill_switch",
            )
            return OrchestratorDecision(status="rejected", reason="operator_kill_switch", opportunity_id=opp.id)

        if str(op_state.get("state", "UNKNOWN")).upper() != "TRADING":
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="operator_not_trading",
            )
            return OrchestratorDecision(status="rejected", reason="operator_not_trading", opportunity_id=opp.id)

        if not self._is_strategy_enabled(op_state):
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="strategy_disabled",
            )
            return OrchestratorDecision(status="rejected", reason="strategy_disabled", opportunity_id=opp.id)

        ok, risk_reason = self._risk_gate(opp)
        if not ok:
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason=risk_reason,
            )
            return OrchestratorDecision(status="rejected", reason=risk_reason, opportunity_id=opp.id)

        token_in = str(opp.constraints.get("token_in") or "")
        token_out = str(opp.constraints.get("token_out") or "")
        ttl_s = int(opp.constraints.get("ttl_s") or self.default_ttl_s)
        slippage_bps = int(opp.constraints.get("slippage_bps") or self.default_slippage_bps)
        if not token_in or not token_out:
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="missing_tokens",
            )
            return OrchestratorDecision(status="rejected", reason="missing_tokens", opportunity_id=opp.id)

        best: Optional[tuple[int, Any, Any, Any, float]] = None
        for size in sorted(set(int(s) for s in opp.size_candidates if int(s) > 0)):
            intent = TradeIntent(
                family=opp.family,
                chain=opp.chain,
                network=opp.network,
                token_in=token_in,
                token_out=token_out,
                amount_in=int(size),
                slippage_bps=slippage_bps,
                ttl_s=ttl_s,
                strategy=self.strategy,
                dex_preference=str(opp.constraints.get("best_dex") or "") or None,
            )
            sel = self.router.route(intent)
            if sel is None:
                continue
            pack = self.registry.get(sel.dex)
            if pack is None:
                continue
            est = self._estimate_profit_after_costs(
                amount_in=size,
                edge_bps=float(opp.expected_edge_bps),
                fee_estimate=float(sel.quote.fee_estimate or 0.0),
            )
            if best is None or est > best[4]:
                best = (size, sel, pack, intent, est)

        if best is None:
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="no_viable_route",
            )
            return OrchestratorDecision(status="rejected", reason="no_viable_route", opportunity_id=opp.id)

        size, sel, pack, intent, est_profit = best

        mode_pick_ctx = {
            "type": opp.type,
            "gas_gwei": float((opp.signals or {}).get("gas_gwei", 0.0)),
            "detected_snipers": int((opp.signals or {}).get("detected_snipers", 0)),
            "vulnerable_flow": bool((opp.signals or {}).get("vulnerable_flow", False)),
            "exact_output": bool((opp.signals or {}).get("exact_output", False)),
        }
        exec_path, exec_reason = self.pick_mode(mode_pick_ctx)
        orchestrator_decisions_total.labels(mode=exec_path, reason=exec_reason).inc()

        if est_profit <= self.min_profit_after_cost:
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="profit_below_threshold",
            )
            return OrchestratorDecision(status="rejected", reason="profit_below_threshold", opportunity_id=opp.id)

        if float(sel.quote.fee_estimate or 0.0) > self.max_fee:
            ops_metrics.record_opportunity_rejected(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                reason="fee_above_max",
            )
            return OrchestratorDecision(status="rejected", reason="fee_above_max", opportunity_id=opp.id)

        ops_metrics.record_opportunity_attempted(
            family=opp.family,
            chain=opp.chain,
            dex=sel.dex,
            strategy=self.strategy,
        )

        built = pack.build(intent, sel.quote)
        simulated_ok = True
        sim_result = None
        if mode in {"paper", "live"}:
            sim_result = pack.simulate(built)
            simulated_ok = bool(getattr(sim_result, "ok", False))
            ops_metrics.record_opportunity_simulated(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                dex=sel.dex,
                ok=simulated_ok,
            )
            if not simulated_ok:
                reason = str(getattr(sim_result, "error_code", "sim_failed") or "sim_failed")
                ops_metrics.record_opportunity_rejected(
                    family=opp.family,
                    chain=opp.chain,
                    strategy=self.strategy,
                    reason=reason,
                )
                ops_metrics.record_dex_sim_fail(
                    family=opp.family,
                    chain=opp.chain,
                    dex=sel.dex,
                    reason=reason,
                )
                return OrchestratorDecision(status="rejected", reason=reason, opportunity_id=opp.id)

        if mode == "live" and not simulated_ok:
            return OrchestratorDecision(status="rejected", reason="live_requires_sim_ok", opportunity_id=opp.id)

        leg = TradeLeg(
            dex=sel.dex,
            token_in=token_in,
            token_out=token_out,
            amount_in=int(size),
            expected_out=int(sel.quote.expected_out),
            min_out=int(sel.quote.min_out),
        )
        plan = TradePlan(
            id=f"plan:{opp.id}",
            ts=time.time(),
            family=opp.family,
            chain=opp.chain,
            network=opp.network,
            opportunity_id=opp.id,
            mode=mode,
            dex_pack=sel.dex,
            ttl_s=ttl_s,
            max_fee=self.max_fee,
            slippage_bps=slippage_bps,
            expected_profit_after_costs=float(est_profit),
            legs=[leg],
            metadata={
                "execution_path": exec_path,
                "execution_reason": exec_reason,
                "opportunity_type": opp.type,
                "refs": dict(opp.refs),
                "constraints": dict(opp.constraints),
                "dex_build": asdict(built),
                "sim_result": asdict(sim_result) if sim_result is not None else None,
            },
        )

        executed = False
        tx_type = str(opp.type)
        if mode == "live" and self.fork_simulator is not None:
            fork_res = self.fork_simulator.simulate(plan)
            fork_ok = bool(getattr(fork_res, "sim_ok", False))
            ops_metrics.record_opportunity_simulated(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
                dex=sel.dex,
                ok=fork_ok,
            )
            if not fork_ok:
                ops_metrics.record_opportunity_rejected(
                    family=opp.family,
                    chain=opp.chain,
                    strategy=self.strategy,
                    reason="fork_sim_failed",
                )
                ops_metrics.record_dex_sim_fail(
                    family=opp.family,
                    chain=opp.chain,
                    dex=sel.dex,
                    reason="fork_sim_failed",
                )
                return OrchestratorDecision(status="rejected", reason="fork_sim_failed", opportunity_id=opp.id)

        if mode == "live" and self.execute_cb is not None:
            try:
                executed = bool(self.execute_cb(plan, asdict(built)))
            except Exception:
                executed = False

        if mode == "live":
            ops_metrics.record_tx_sent(
                family=opp.family,
                chain=opp.chain,
                strategy=self.strategy,
            )
            ops_metrics.record_tx_sent_by_dex_type(
                family=opp.family,
                chain=opp.chain,
                dex=sel.dex,
                tx_type=tx_type,
            )

        ops_metrics.record_opportunity_executed(
            family=opp.family,
            chain=opp.chain,
            strategy=self.strategy,
            dex=sel.dex,
            mode=mode,
        )
        if mode in {"paper", "live"}:
            ops_metrics.record_opportunity_filled(
                family=opp.family,
                chain=opp.chain,
                dex=sel.dex,
                strategy=self.strategy,
            )

        return OrchestratorDecision(
            status="planned" if mode != "live" else ("executed" if executed else "planned"),
            reason="ok",
            opportunity_id=opp.id,
            plan=plan,
        )


# Backward-compatible export alias.
OpportunityOrchestrator = UnifiedOrchestrator


def build_orchestrator(
    *,
    router: TradeRouter,
    registry: DEXPackRegistry,
    detectors: List[BaseDetector],
    strategy: str = "opportunity_engine",
    execute_cb: Optional[Callable[[TradePlan, Dict[str, Any]], bool]] = None,
    operator_state_path: Optional[str] = None,
    mode_cfg: Optional[OrchestratorConfig] = None,
    fork_simulator: Optional[ForkSimulator] = None,
) -> UnifiedOrchestrator:
    sim_backend = str(os.getenv("SIM_BACKEND", "heuristic")).strip().lower() or "heuristic"
    selected_fork_sim = fork_simulator if sim_backend == "fork" else None
    if sim_backend == "fork" and selected_fork_sim is None:
        selected_fork_sim = ForkSimulator()
    return UnifiedOrchestrator(
        router=router,
        registry=registry,
        detectors=detectors,
        strategy=strategy,
        execute_cb=execute_cb,
        operator_state_path=operator_state_path,
        mode_cfg=mode_cfg,
        fork_simulator=selected_fork_sim,
    )

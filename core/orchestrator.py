from __future__ import annotations

import heapq
import os
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from bot.core.opportunity_engine.scoring import freshness_weight
from bot.core.opportunity_engine.types import Opportunity, TradeLeg, TradePlan
from bot.core.operator_control import get_operator_state
from bot.core.types_dex import TradeIntent
from adapters.flashloans.base import FlashloanProvider
from core.risk_gates import RiskGateConfig, cheap_opportunity_gate, operator_gate
from core.sizing import CostModel, search_best_size
from ops import metrics as ops_metrics
from strategies.flashloan_arb import FlashloanArbStrategy


@dataclass(frozen=True)
class OrchestratorDecision:
    status: str
    reason: str
    opportunity_id: str
    plan: Optional[TradePlan] = None


@dataclass
class _QueueItem:
    priority: float
    enqueued_ts: float
    opportunity: Opportunity
    profit_est_usd: float


class OpportunityOrchestrator:
    def __init__(
        self,
        *,
        router: Any,
        registry: Any,
        strategy: str = "opportunity_engine",
        execute_cb: Optional[Callable[[TradePlan, Dict[str, Any]], bool]] = None,
        operator_state_path: Optional[str] = None,
        flashloan_provider: Optional[FlashloanProvider] = None,
        flashloan_arb_strategy: Optional[FlashloanArbStrategy] = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.strategy = str(strategy or "opportunity_engine")
        self.execute_cb = execute_cb
        self.operator_state_path = operator_state_path
        self.flashloan_provider = flashloan_provider
        self.flashloan_arb_strategy = flashloan_arb_strategy or (
            FlashloanArbStrategy(flashloan_provider) if flashloan_provider is not None else None
        )
        self.risk_cfg = RiskGateConfig.from_env()
        self.min_profit_after_cost = float(os.getenv("MIN_PROFIT_AFTER_COST", "0.01"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))
        self.window_s = max(60.0, float(os.getenv("ORCH_WINDOW_S", "600")))
        self.inventory_usd = float(os.getenv("INVENTORY_USD", "0.0"))
        self.flashloan_gas_overhead_usd = float(os.getenv("FLASHLOAN_GAS_OVERHEAD_USD", "0.0"))
        self.flashloan_min_size = float(os.getenv("FLASHLOAN_MIN_SIZE_USD", "0.0"))
        self.cost_model = CostModel(
            gas_cost_usd=float(os.getenv("OPP_GAS_COST_EST", "0.0")),
            fee_bps=float(os.getenv("OPP_FEE_BPS", "0.0")),
            flashloan_fee_bps=float(os.getenv("OPP_FLASHLOAN_FEE_BPS", "0.0")),
        )
        self._pq: List[Tuple[float, float, _QueueItem]] = []
        self._events: Deque[tuple[float, str]] = deque(maxlen=20_000)
        self._rejects: Deque[tuple[float, str]] = deque(maxlen=20_000)

    def _should_use_flashloan(self, *, size_usd: float, est_profit_usd: float) -> tuple[bool, float]:
        p = self.flashloan_provider
        if p is None:
            return False, 0.0
        if size_usd <= max(0.0, self.inventory_usd):
            return False, 0.0
        if size_usd < max(0.0, self.flashloan_min_size):
            return False, 0.0
        fee_est = (float(size_usd) * max(0.0, float(p.fee_bps())) / 10_000.0) + max(0.0, self.flashloan_gas_overhead_usd)
        if float(est_profit_usd) <= fee_est:
            return False, fee_est
        return True, fee_est

    def _profit_est_usd(self, opp: Opportunity) -> float:
        for src in (opp.constraints, opp.refs):
            v = src.get("profit_est_usd")
            if v is not None:
                try:
                    return max(0.0, float(v))
                except Exception:
                    pass
        base = int(max(1, min(int(x) for x in opp.size_candidates if int(x) > 0)))
        return float(base) * max(0.0, float(opp.expected_edge_bps)) / 10_000.0

    def _priority(self, opp: Opportunity, *, now: Optional[float] = None) -> float:
        p = self._profit_est_usd(opp)
        c = max(0.0, min(1.0, float(opp.confidence)))
        f = freshness_weight(opp.ts, now=now)
        return float(p * c * f)

    def _record_event(self, kind: str, *, ts: Optional[float] = None) -> None:
        self._events.append((float(time.time() if ts is None else ts), kind))

    def _record_reject(self, reason: str, *, ts: Optional[float] = None) -> None:
        now = float(time.time() if ts is None else ts)
        self._record_event("rejected", ts=now)
        self._rejects.append((now, str(reason or "unknown")))

    def enqueue(self, opp: Opportunity) -> None:
        now = time.time()
        pr = self._priority(opp, now=now)
        item = _QueueItem(priority=pr, enqueued_ts=now, opportunity=opp, profit_est_usd=self._profit_est_usd(opp))
        heapq.heappush(self._pq, (-pr, now, item))
        self._record_event("seen", ts=now)
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

    def _pop(self) -> Optional[_QueueItem]:
        if not self._pq:
            return None
        _, _, item = heapq.heappop(self._pq)
        ops_metrics.set_opportunity_queue_depth(
            family=item.opportunity.family,
            chain=item.opportunity.chain,
            strategy=self.strategy,
            depth=len(self._pq),
        )
        return item

    def process_next(self) -> OrchestratorDecision:
        item = self._pop()
        if item is None:
            return OrchestratorDecision(status="empty", reason="queue_empty", opportunity_id="")
        return self.process(item.opportunity)

    def process(self, opp: Opportunity) -> OrchestratorDecision:
        op_state = get_operator_state(path=self.operator_state_path)
        mode = str(op_state.get("mode", "dryrun")).strip().lower() or "dryrun"
        strategy_label = (
            "flashloan_arb"
            if (self.flashloan_arb_strategy is not None and str(opp.type).strip().lower() in {"xarb", "triarb"})
            else self.strategy
        )

        def _rej(reason: str) -> OrchestratorDecision:
            ops_metrics.record_opportunity_rejected(
                family=opp.family, chain=opp.chain, strategy=strategy_label, reason=reason
            )
            self._record_reject(reason)
            return OrchestratorDecision(status="rejected", reason=reason, opportunity_id=opp.id)

        op_gate = operator_gate(op_state)
        if not op_gate.ok:
            return _rej(op_gate.reason)

        cheap = cheap_opportunity_gate(opp, self.risk_cfg)
        if not cheap.ok:
            return _rej(cheap.reason)

        token_in = str(opp.constraints.get("token_in") or "")
        token_out = str(opp.constraints.get("token_out") or "")
        slippage_bps = int(opp.constraints.get("slippage_bps") or self.default_slippage_bps)
        ttl_s = int(opp.constraints.get("ttl_s") or self.default_ttl_s)
        dex_pref = str(opp.constraints.get("best_dex") or "").strip().lower() or None

        last_sel: Any = None
        last_pack: Any = None
        last_intent: Optional[TradeIntent] = None
        last_profit: float = float("-inf")

        def _score_size(size: int) -> float:
            nonlocal last_sel, last_pack, last_intent, last_profit
            intent = TradeIntent(
                family=opp.family,
                chain=opp.chain,
                network=opp.network,
                token_in=token_in,
                token_out=token_out,
                amount_in=int(size),
                slippage_bps=slippage_bps,
                ttl_s=ttl_s,
                strategy=strategy_label,
                dex_preference=dex_pref,
            )
            sel = self.router.route(intent)
            if sel is None:
                return float("-inf")
            pack = self.registry.get(sel.dex)
            if pack is None:
                return float("-inf")
            net = self.cost_model.net_profit_usd(
                amount_in=int(size),
                edge_bps=float(opp.expected_edge_bps),
                quote_fee_usd=float(sel.quote.fee_estimate or 0.0),
            )
            if net > last_profit:
                last_profit = net
                last_sel = sel
                last_pack = pack
                last_intent = intent
            return net

        sized = search_best_size(opp.size_candidates, _score_size)
        if sized.best_size <= 0 or last_sel is None or last_pack is None or last_intent is None:
            return _rej("no_viable_route")

        if float(last_profit) < max(self.min_profit_after_cost, self.risk_cfg.min_profit_est_usd):
            return _rej("profit_below_threshold")

        ops_metrics.record_opportunity_attempted(
            family=opp.family, chain=opp.chain, dex=last_sel.dex, strategy=strategy_label
        )

        try:
            built = last_pack.build(last_intent, last_sel.quote)
        except Exception:
            return _rej("build_failed")

        use_fl, fl_fee_usd = self._should_use_flashloan(
            size_usd=float(sized.best_size), est_profit_usd=float(last_profit)
        )
        arb_plan = None
        if use_fl and self.flashloan_provider is not None:
            try:
                adjusted_profit = float(last_profit) - float(fl_fee_usd)
                if adjusted_profit < max(self.min_profit_after_cost, self.risk_cfg.min_profit_est_usd):
                    return _rej("flashloan_profit_below_threshold")
                if self.flashloan_arb_strategy is not None and str(opp.type).strip().lower() in {"xarb", "triarb"}:
                    arb_plan = self.flashloan_arb_strategy.build_plan(
                        opportunity=opp,
                        size=int(sized.best_size),
                        expected_profit_after_costs=adjusted_profit,
                    )
                built = self.flashloan_provider.build_flashloan_wrapper(built)
                if arb_plan is not None and self.flashloan_arb_strategy is not None:
                    built = self.flashloan_arb_strategy.attach_to_txplan(built, arb_plan)
                ops_metrics.record_flashloan_used(
                    family=opp.family,
                    chain=opp.chain,
                    provider=self.flashloan_provider.name(),
                )
                ops_metrics.record_flashloan_fee_est_usd(
                    family=opp.family,
                    chain=opp.chain,
                    provider=self.flashloan_provider.name(),
                    usd=float(fl_fee_usd),
                )
                last_profit = adjusted_profit
            except Exception:
                use_fl = False

        sim_ok = True
        sim_result = None
        if mode in {"paper", "live"}:
            sim_result = last_pack.simulate(built)
            sim_ok = bool(getattr(sim_result, "ok", False))
            ops_metrics.record_opportunity_simulated(
                family=opp.family,
                chain=opp.chain,
                strategy=strategy_label,
                dex=last_sel.dex,
                ok=sim_ok,
            )
            self._record_event("simulated")
            if not sim_ok:
                raw_code = str(getattr(sim_result, "error_code", "sim_failed") or "sim_failed")
                raw_msg = str(getattr(sim_result, "error_message", "") or "")
                raw_logs = list(getattr(sim_result, "logs", None) or [])
                reason = raw_code
                if use_fl and self.flashloan_arb_strategy is not None:
                    reason = self.flashloan_arb_strategy.bucket_sim_failure(
                        error_code=raw_code,
                        error_message=raw_msg,
                        logs=raw_logs,
                    )
                ops_metrics.record_sim_fail(
                    family=opp.family,
                    chain=opp.chain,
                    strategy=strategy_label,
                    reason=reason,
                )
                return _rej(reason)

        leg = TradeLeg(
            dex=last_sel.dex,
            token_in=token_in,
            token_out=token_out,
            amount_in=int(sized.best_size),
            expected_out=int(last_sel.quote.expected_out),
            min_out=int(last_sel.quote.min_out),
        )
        plan = TradePlan(
            id=f"plan:{opp.id}",
            ts=time.time(),
            family=opp.family,
            chain=opp.chain,
            network=opp.network,
            opportunity_id=opp.id,
            mode=mode,
            dex_pack=last_sel.dex,
            ttl_s=ttl_s,
            max_fee=float(self.risk_cfg.max_fee_usd),
            slippage_bps=slippage_bps,
            expected_profit_after_costs=float(last_profit),
            legs=[leg],
            metadata={
                "quote_table": [asdict(x) for x in getattr(last_sel, "quote_table", getattr(last_sel, "candidates", []))],
                "sizing_evaluated": list(sized.evaluated),
                "sim_result": asdict(sim_result) if sim_result is not None else None,
                "flashloan": {
                    "used": bool(use_fl),
                    "provider": self.flashloan_provider.name() if (use_fl and self.flashloan_provider is not None) else None,
                    "fee_est_usd": float(fl_fee_usd),
                },
                "strategy": strategy_label,
                "opportunity_refs": dict(opp.refs),
            },
        )

        executed = False
        if mode == "live" and self.execute_cb is not None:
            try:
                executed = bool(self.execute_cb(plan, asdict(built)))
            except Exception:
                executed = False

        ops_metrics.record_opportunity_executed(
            family=opp.family, chain=opp.chain, strategy=strategy_label, dex=last_sel.dex, mode=mode
        )
        self._record_event("executed")
        if mode in {"paper", "live"}:
            ops_metrics.record_opportunity_filled(
                family=opp.family, chain=opp.chain, dex=last_sel.dex, strategy=strategy_label
            )
        if mode == "live":
            ops_metrics.record_tx_sent(family=opp.family, chain=opp.chain, strategy=strategy_label)
            ops_metrics.record_tx_sent_by_dex_type(
                family=opp.family, chain=opp.chain, dex=last_sel.dex, tx_type=str(opp.type)
            )
        return OrchestratorDecision(
            status="executed" if (mode == "live" and executed) else "planned",
            reason="ok",
            opportunity_id=opp.id,
            plan=plan,
        )

    def snapshot(self, *, top_n: int = 5, now: Optional[float] = None) -> Dict[str, Any]:
        t = float(time.time() if now is None else now)
        cutoff = t - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        while self._rejects and self._rejects[0][0] < cutoff:
            self._rejects.popleft()

        funnel = {"seen": 0, "rejected": 0, "simulated": 0, "executed": 0}
        for _, kind in self._events:
            if kind in funnel:
                funnel[kind] += 1
        pareto = dict(Counter([reason for _, reason in self._rejects]).most_common(5))

        items = sorted(self._pq, key=lambda x: (x[0], x[1]))
        top: List[Dict[str, Any]] = []
        for neg_prio, _, qi in items[: max(1, int(top_n))]:
            top.append(
                {
                    "id": qi.opportunity.id,
                    "type": qi.opportunity.type,
                    "family": qi.opportunity.family,
                    "chain": qi.opportunity.chain,
                    "network": qi.opportunity.network,
                    "priority": float(-neg_prio),
                    "profit_est_usd": float(qi.profit_est_usd),
                    "confidence": float(qi.opportunity.confidence),
                    "age_s": max(0.0, t - float(qi.opportunity.ts)),
                }
            )
        return {
            "ts": int(t),
            "queue_depth": len(self._pq),
            "top_opportunities": top,
            "funnel_10m": funnel,
            "reject_reasons_pareto_10m": pareto,
        }

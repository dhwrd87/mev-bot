"""Enhanced trading orchestrator for strategic decisioning and execution.

This module contains the high-level control plane for opportunity handling.
It evaluates operator controls, selects execution strategy, applies risk
constraints, executes strategy calls, records trade outcomes, and emits
orchestrator metrics.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from prometheus_client import Counter, Histogram, REGISTRY

from bot.core.chain_config import get_chain_config
from bot.core.config import AppSettings as Settings
from bot.core.config import get_settings
from bot.ports.real import RealRpcClient, RealTradeRepo
from bot.risk.adaptive import AdaptiveRiskManager, RiskConfig
from bot.strategy.base import TransactionResult
from bot.strategy.hunter import HunterStrategy
from bot.strategy.stealth import StealthStrategy

LOG = logging.getLogger("trading-orchestrator")


def _counter(name: str, documentation: str, labels: list[str]) -> Counter:
    try:
        return Counter(name, documentation, labels)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing  # type: ignore[return-value]


def _histogram(name: str, documentation: str, labels: list[str], buckets: list[float]) -> Histogram:
    try:
        return Histogram(name, documentation, labels, buckets=buckets)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing  # type: ignore[return-value]


orchestrator_decisions_total = _counter(
    "orchestrator_decisions_total",
    "Total orchestrator strategy decisions",
    ["family", "chain", "mode", "reason"],
)
orchestrator_executions_total = _counter(
    "orchestrator_executions_total",
    "Total orchestrator execution attempts",
    ["family", "chain", "mode", "strategy"],
)
orchestrator_execution_results_total = _counter(
    "orchestrator_execution_results_total",
    "Total orchestrator execution results",
    ["family", "chain", "mode", "strategy", "outcome"],
)
orchestrator_risk_blocks_total = _counter(
    "orchestrator_risk_blocks_total",
    "Total opportunities blocked by risk gates",
    ["family", "chain", "reason"],
)
decision_latency_ms = _histogram(
    "decision_latency_ms",
    "Orchestrator decision latency in milliseconds",
    ["family", "chain", "mode"],
    buckets=[1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000],
)
execution_latency_ms = _histogram(
    "execution_latency_ms",
    "Orchestrator execution latency in milliseconds",
    ["family", "chain", "mode", "strategy"],
    buckets=[1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, default=str)
    except Exception:
        return "{}"


def _read_operator_state(path: str) -> Dict[str, str]:
    """Load operator state JSON and normalize values to strings."""
    defaults: Dict[str, str] = {
        "paused": "false",
        "kill_switch": "false",
        "state": "TRADING",
        "mode": "paper",
    }
    p = Path(path)
    if not p.exists():
        return defaults
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return defaults
        merged = dict(defaults)
        merged.update({str(k): str(v) for k, v in payload.items()})
        # Some state files use booleans for these keys.
        merged["paused"] = str(payload.get("paused", merged["paused"])).lower()
        merged["kill_switch"] = str(payload.get("kill_switch", merged["kill_switch"])).lower()
        merged["state"] = str(payload.get("state", merged["state"])).upper()
        return merged
    except Exception as exc:
        LOG.warning("operator_state_load_failed path=%s err=%s", path, exc)
        return defaults


@dataclass
class DecisionContext:
    """Decision input envelope used across strategy selection and risk gates."""

    opportunity: Dict[str, Any]
    family: str
    chain: str
    network: str
    operator_state: Dict[str, str]
    risk_state: Dict[str, Any]
    timestamp: float


@dataclass
class ExecutionResult:
    """Full execution outcome returned by ``TradingOrchestrator.handle_opportunity``."""

    executed: bool
    mode: str
    strategy: str
    reason: str
    trade_id: Optional[int]
    tx_hash: Optional[str]
    bundle_tag: Optional[str]
    expected_profit_usd: float
    realized_profit_usd: float
    gas_cost_usd: float
    slippage_bps: float
    latency_ms: float
    error: Optional[str]
    metadata: Optional[Dict[str, Any]] = field(default=None)


class TradeRecorder:
    """Persistence adapter for trade lifecycle records."""

    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        self._repo = RealTradeRepo()

    async def record_trade(self, row: Dict[str, Any]) -> Optional[int]:
        """Persist trade row and return trade id when available."""
        try:
            return await self._repo.insert_trade(row)
        except Exception as exc:
            LOG.exception("trade_record_failed err=%s row=%s", exc, _safe_json(row))
            return None


class TradingOrchestrator:
    """Brain of the trading system that performs all strategic decisions.

    Decision flow:
    1. Build contextual view of operator and risk state.
    2. Select strategy and execution mode from opportunity features.
    3. Apply risk gates and determine approved notional size.
    4. Execute strategy and transform transaction result.
    5. Persist trade outcome and update risk memory.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        stealth_strategy: StealthStrategy,
        hunter_strategy: HunterStrategy,
        risk_manager: AdaptiveRiskManager,
        trade_recorder: TradeRecorder,
        operator_state_path: str = "runtime/operator_state_runtime.json",
    ) -> None:
        self.settings = settings
        self.stealth = stealth_strategy
        self.hunter = hunter_strategy
        self.risk_manager = risk_manager
        self.trade_recorder = trade_recorder
        self.operator_state_path = operator_state_path

        self.gas_spike_threshold_gwei = float(os.getenv("GAS_SPIKE_THRESHOLD_GWEI", "120.0"))
        self.stealth_slippage_threshold = float(os.getenv("STEALTH_SLIPPAGE_THRESHOLD", "0.005"))
        self.hunter_min_profit_usd = float(os.getenv("HUNTER_MIN_PROFIT_USD", "5.0"))

    async def handle_opportunity(self, opportunity: Dict[str, Any]) -> ExecutionResult:
        """Handle one opportunity end-to-end and return structured result."""
        started = time.perf_counter()
        family = str(opportunity.get("family") or os.getenv("CHAIN_FAMILY", "evm")).strip().lower()
        chain = str(opportunity.get("chain") or os.getenv("CHAIN", "unknown")).strip().lower()
        network = str(opportunity.get("network") or ("mainnet" if "mainnet" in chain else "testnet")).strip().lower()
        opportunity_id = str(opportunity.get("id") or opportunity.get("hash") or f"opp-{int(time.time() * 1000)}")

        operator_state = _read_operator_state(self.operator_state_path)
        risk_state = {
            "daily_pnl": _as_float(getattr(self.risk_manager, "daily_pnl", 0.0), 0.0),
            "consecutive_losses": _as_int(getattr(self.risk_manager, "consecutive_losses", 0), 0),
        }
        ctx = DecisionContext(
            opportunity=dict(opportunity),
            family=family,
            chain=chain,
            network=network,
            operator_state=operator_state,
            risk_state=risk_state,
            timestamp=time.time(),
        )

        mode, strategy, reason = self._select_strategy(ctx)
        orchestrator_decisions_total.labels(family=family, chain=chain, mode=mode, reason=reason).inc()
        decision_ms = (time.perf_counter() - started) * 1000.0
        decision_latency_ms.labels(family=family, chain=chain, mode=mode).observe(decision_ms)
        with __import__("contextlib").suppress(Exception):
            from bot.core.telemetry_trading import record_strategy_decision

            record_strategy_decision(family, chain, mode, strategy, reason, decision_ms)
        LOG.info(
            "decision opportunity_id=%s mode=%s strategy=%s reason=%s family=%s chain=%s expected_profit_usd=%.4f",
            opportunity_id,
            mode,
            strategy,
            reason,
            family,
            chain,
            _as_float(opportunity.get("expected_profit_usd"), 0.0),
        )

        approved, risk_reason, approved_size = self._apply_risk_gates(ctx, mode, strategy)
        with __import__("contextlib").suppress(Exception):
            from bot.core.telemetry_trading import record_risk_decision

            record_risk_decision(
                family=family,
                chain=chain,
                mode=mode,
                approved=approved,
                reason=risk_reason,
                size_usd=approved_size,
                strategy=strategy,
            )
        if not approved:
            latency_ms = (time.perf_counter() - started) * 1000.0
            orchestrator_risk_blocks_total.labels(family=family, chain=chain, reason=risk_reason).inc()
            final_reason = (
                reason
                if mode == "none" and reason in {"operator_paused", "kill_switch_active"}
                else risk_reason
            )
            return ExecutionResult(
                executed=False,
                mode=mode,
                strategy=strategy,
                reason=final_reason,
                trade_id=None,
                tx_hash=None,
                bundle_tag=None,
                expected_profit_usd=_as_float(opportunity.get("expected_profit_usd"), 0.0),
                realized_profit_usd=0.0,
                gas_cost_usd=0.0,
                slippage_bps=_as_float(opportunity.get("estimated_slippage"), 0.0) * 10_000.0,
                latency_ms=latency_ms,
                error=None,
                metadata={"opportunity_id": opportunity_id, "risk_state": risk_state},
            )

        result = await self._execute_trade(ctx, mode, strategy, approved_size)
        if result.executed:
            # Preserve strategic decision reason on successful execution.
            result.reason = reason
        result.latency_ms = (time.perf_counter() - started) * 1000.0
        with __import__("contextlib").suppress(Exception):
            from bot.core.telemetry_trading import record_execution_result

            record_execution_result(
                family=family,
                chain=chain,
                mode=mode,
                strategy=strategy,
                success=result.executed,
                expected_profit=result.expected_profit_usd,
                realized_profit=result.realized_profit_usd,
                gas_cost=result.gas_cost_usd,
                slippage_bps=result.slippage_bps,
                latency_ms=result.latency_ms,
                dex=str(opportunity.get("dex") or "unknown"),
            )

        trade_row = {
            "mode": mode,
            "chain": chain,
            "token_in": opportunity.get("token_in"),
            "token_out": opportunity.get("token_out"),
            "pair": f"{opportunity.get('token_in', '')}-{opportunity.get('token_out', '')}",
            "size_usd": approved_size,
            "expected_profit_usd": result.expected_profit_usd,
            "realized_pnl_usd": result.realized_profit_usd,
            "gas_usd": result.gas_cost_usd,
            "status": "submitted" if result.executed else "failed",
            "tx_hash": result.tx_hash,
            "bundle_tag": result.bundle_tag,
            "builder": None,
            "context": {
                "opportunity_id": opportunity_id,
                "strategy": strategy,
                "reason": result.reason,
                "error": result.error,
            },
        }
        result.trade_id = await self.trade_recorder.record_trade(trade_row)

        if hasattr(self.risk_manager, "record_trade_result"):
            try:
                self.risk_manager.record_trade_result(result.realized_profit_usd)
            except Exception as exc:
                LOG.warning("risk_record_trade_result_failed err=%s", exc)
        elif hasattr(self.risk_manager, "record_result"):
            try:
                self.risk_manager.record_result(result.realized_profit_usd)
            except Exception as exc:
                LOG.warning("risk_record_result_failed err=%s", exc)
        with __import__("contextlib").suppress(Exception):
            from bot.core.telemetry_trading import update_cumulative_pnl

            cumulative = float(getattr(self.risk_manager, "daily_pnl", 0.0))
            update_cumulative_pnl(family, chain, mode, cumulative)

        LOG.info(
            "opportunity_processed opportunity_id=%s executed=%s mode=%s strategy=%s trade_id=%s tx_hash=%s bundle_tag=%s "
            "expected_profit_usd=%.4f realized_profit_usd=%.4f gas_cost_usd=%.4f latency_ms=%.2f error=%s",
            opportunity_id,
            result.executed,
            mode,
            strategy,
            result.trade_id,
            result.tx_hash,
            result.bundle_tag,
            result.expected_profit_usd,
            result.realized_profit_usd,
            result.gas_cost_usd,
            result.latency_ms,
            result.error,
        )
        return result

    def _select_strategy(self, ctx: DecisionContext) -> Tuple[str, str, str]:
        """Select mode + strategy according to deterministic ordered rules."""
        opp = ctx.opportunity
        state = ctx.operator_state

        if _to_bool(opp.get("force_stealth")):
            return "stealth", "stealth_exact_output", "forced"
        if _to_bool(opp.get("force_hunter")):
            return "hunter", "hunter_backrun", "forced"
        if _to_bool(state.get("paused")):
            return "none", "paused", "operator_paused"
        if _to_bool(state.get("kill_switch")):
            return "none", "killed", "kill_switch_active"

        gas_gwei = _as_float(opp.get("gas_gwei"), 0.0)
        if gas_gwei >= self.gas_spike_threshold_gwei:
            return "stealth", "stealth_private", "gas_spike"

        estimated_slippage = _as_float(opp.get("estimated_slippage"), 0.0)
        if estimated_slippage >= self.stealth_slippage_threshold:
            return "stealth", "stealth_exact_output", "high_slippage_risk"

        if _as_int(opp.get("detected_snipers"), 0) > 0 and bool(opp.get("vulnerable_flow")):
            return "hunter", "hunter_backrun", "sniper_opportunity"

        if _as_float(opp.get("token_age_hours"), 10_000.0) < 24.0:
            return "stealth", "stealth_private", "new_token"

        if str(opp.get("type") or "").lower() in {"xarb", "triarb"} and _as_float(
            opp.get("expected_profit_usd"), 0.0
        ) >= self.hunter_min_profit_usd:
            return "hunter", "hunter_arb", "arbitrage"

        return "stealth", "stealth_default", "default_safe"

    def _apply_risk_gates(self, ctx: DecisionContext, mode: str, strategy: str) -> Tuple[bool, str, float]:
        """Apply risk policy and return ``(approved, reason, approved_size_usd)``."""
        _ = strategy
        if mode not in {"stealth", "hunter"}:
            return False, "mode_not_executable", 0.0

        opp = dict(ctx.opportunity)
        requested_size = _as_float(opp.get("size_usd", opp.get("approved_size_usd", 0.0)), 0.0)
        expected_profit = _as_float(opp.get("expected_profit_usd"), 0.0)

        if requested_size <= 0:
            return False, "invalid_size", 0.0

        if hasattr(self.risk_manager, "approve_trade"):
            try:
                decision = self.risk_manager.approve_trade(
                    mode=mode,
                    strategy=strategy,
                    size_usd=requested_size,
                    expected_profit=expected_profit,
                    opportunity=opp,
                )
                if isinstance(decision, tuple) and len(decision) >= 3:
                    approved = bool(decision[0])
                    reason = str(decision[1])
                    approved_size = _as_float(decision[2], requested_size)
                    return approved, reason, approved_size
            except Exception as exc:
                return False, f"risk_manager_error:{exc}", 0.0

        # Compatibility path for existing AdaptiveRiskManager.should_execute.
        approved_size = requested_size
        if hasattr(self.risk_manager, "position_cap"):
            with_approved = _as_float(self.risk_manager.position_cap(requested_size), requested_size)
            approved_size = max(0.0, with_approved)

        ok = True
        reason = "ok"
        if hasattr(self.risk_manager, "should_execute"):
            try:
                test_opp = dict(opp)
                test_opp["size_usd"] = approved_size
                ok, reason = self.risk_manager.should_execute(test_opp)
            except Exception as exc:
                return False, f"risk_manager_error:{exc}", 0.0

        return bool(ok), str(reason), approved_size if ok else 0.0

    async def _execute_trade(
        self,
        ctx: DecisionContext,
        mode: str,
        strategy: str,
        size_usd: float,
    ) -> ExecutionResult:
        """Execute selected strategy and convert raw tx result to ``ExecutionResult``."""
        family = ctx.family
        chain = ctx.chain
        opp = dict(ctx.opportunity)
        opp["approved_size_usd"] = size_usd
        opp["size_usd"] = size_usd

        if mode not in {"stealth", "hunter"}:
            return ExecutionResult(
                executed=False,
                mode=mode,
                strategy=strategy,
                reason="not_executable",
                trade_id=None,
                tx_hash=None,
                bundle_tag=None,
                expected_profit_usd=_as_float(opp.get("expected_profit_usd"), 0.0),
                realized_profit_usd=0.0,
                gas_cost_usd=0.0,
                slippage_bps=_as_float(opp.get("estimated_slippage"), 0.0) * 10_000.0,
                latency_ms=0.0,
                error=None,
                metadata={"opportunity": opp},
            )

        impl = self.stealth if mode == "stealth" else self.hunter
        orchestrator_executions_total.labels(family=family, chain=chain, mode=mode, strategy=strategy).inc()
        with __import__("contextlib").suppress(Exception):
            from bot.core.telemetry_trading import executions_attempted_total

            executions_attempted_total.labels(family, chain, mode, strategy).inc()
        started = time.perf_counter()
        try:
            tx_result: TransactionResult = await impl.execute(opp)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            execution_latency_ms.labels(family=family, chain=chain, mode=mode, strategy=strategy).observe(elapsed_ms)

            notes = dict(tx_result.notes or {})
            bundle_tag = str(notes.get("bundle_tag") or "") or None
            outcome = "success" if tx_result.success else "failed"
            orchestrator_execution_results_total.labels(
                family=family,
                chain=chain,
                mode=mode,
                strategy=strategy,
                outcome=outcome,
            ).inc()
            return ExecutionResult(
                executed=bool(tx_result.success),
                mode=mode,
                strategy=strategy,
                reason="ok" if tx_result.success else str(notes.get("reason") or "execution_failed"),
                trade_id=None,
                tx_hash=(tx_result.tx_hash or None),
                bundle_tag=bundle_tag,
                expected_profit_usd=_as_float(opp.get("expected_profit_usd"), 0.0),
                realized_profit_usd=_as_float(notes.get("realized_profit_usd"), 0.0),
                gas_cost_usd=_as_float(notes.get("gas_cost_usd"), 0.0),
                slippage_bps=float(tx_result.slippage) * 10_000.0,
                latency_ms=elapsed_ms,
                error=None if tx_result.success else str(notes.get("error") or "execution_failed"),
                metadata=notes,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            execution_latency_ms.labels(family=family, chain=chain, mode=mode, strategy=strategy).observe(elapsed_ms)
            orchestrator_execution_results_total.labels(
                family=family,
                chain=chain,
                mode=mode,
                strategy=strategy,
                outcome="error",
            ).inc()
            LOG.exception(
                "execution_exception mode=%s strategy=%s chain=%s family=%s err=%s opportunity=%s",
                mode,
                strategy,
                chain,
                family,
                exc,
                _safe_json(opp),
            )
            return ExecutionResult(
                executed=False,
                mode=mode,
                strategy=strategy,
                reason="exception",
                trade_id=None,
                tx_hash=None,
                bundle_tag=None,
                expected_profit_usd=_as_float(opp.get("expected_profit_usd"), 0.0),
                realized_profit_usd=0.0,
                gas_cost_usd=0.0,
                slippage_bps=_as_float(opp.get("estimated_slippage"), 0.0) * 10_000.0,
                latency_ms=elapsed_ms,
                error=str(exc),
                metadata={"opportunity": opp},
            )


class _SignerStub:
    async def sign_backrun(self, opp_ctx: Dict[str, Any]) -> str:
        suffix = str(opp_ctx.get("type") or "arb")
        return f"0xOUR_BACKRUN_{suffix}"


async def create_orchestrator() -> TradingOrchestrator:
    """Factory that builds a fully-wired ``TradingOrchestrator`` from environment."""
    settings = get_settings()
    cfg = get_chain_config()

    stealth = StealthStrategy()
    rpc_client = RealRpcClient(http_url=cfg.rpc_http)
    signer = _SignerStub()
    hunter = HunterStrategy(chain=cfg.chain, signer=signer, rpc_client=rpc_client)

    capital_usd = float(os.getenv("RISK_CAPITAL_USD", "10000"))
    max_pos_pct = float(settings.risk.max_position_size) * 100.0
    max_daily_loss_usd = float(settings.risk.max_daily_loss) * capital_usd
    max_consecutive_losses = int(os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "5"))

    risk_cfg = RiskConfig(
        capital_usd=capital_usd,
        max_position_size_pct=max_pos_pct,
        max_daily_loss_usd=max_daily_loss_usd,
        max_consecutive_losses=max_consecutive_losses,
    )
    risk_manager = AdaptiveRiskManager(risk_cfg)

    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    trade_recorder = TradeRecorder(database_url=database_url)

    return TradingOrchestrator(
        settings=settings,
        stealth_strategy=stealth,
        hunter_strategy=hunter,
        risk_manager=risk_manager,
        trade_recorder=trade_recorder,
        operator_state_path=os.getenv("OPERATOR_STATE_PATH", "runtime/operator_state_runtime.json"),
    )

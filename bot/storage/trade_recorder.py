"""Trade recording and strategy performance persistence.

This module stores detailed per-trade decision/execution records and maintains
daily strategy aggregates used for analysis and reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import psycopg
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from bot.orchestration.trading_orchestrator import ExecutionResult

LOG = logging.getLogger("trade-recorder")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class TradeRecorder:
    """Persist trading decisions/results and strategy aggregates to PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        self.database_url = self._resolve_database_url(database_url)
        self._trade_columns: set[str] = set()
        self._ensure_tables()
        self._refresh_schema_cache()

    @staticmethod
    def _resolve_database_url(database_url: str) -> str:
        """Resolve a usable DSN from explicit arg or environment fallbacks."""
        explicit = str(database_url or "").strip()
        if explicit:
            return explicit
        env_dsn = str(os.getenv("DATABASE_URL", "")).strip()
        if env_dsn:
            return env_dsn
        db = os.getenv("POSTGRES_DB", "mev_bot")
        host = os.getenv("POSTGRES_HOST", "postgres")
        port = os.getenv("POSTGRES_PORT", "5432")
        user = os.getenv("POSTGRES_USER", "mev_user")
        pwd = os.getenv("POSTGRES_PASSWORD", "")
        return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

    def _connect(self):
        """Open psycopg connection with autocommit enabled."""
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def _ensure_tables(self) -> None:
        """Create required schema objects for trade and aggregate persistence."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        opportunity_id TEXT,
                        opportunity_type TEXT,
                        detector TEXT,
                        family TEXT NOT NULL,
                        chain TEXT NOT NULL,
                        network TEXT,
                        mode TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        decision_reason TEXT,
                        decision_latency_ms FLOAT,
                        executed BOOLEAN NOT NULL,
                        execution_reason TEXT,
                        execution_latency_ms FLOAT,
                        tx_hash TEXT,
                        bundle_tag TEXT,
                        relay TEXT,
                        token_in TEXT,
                        token_out TEXT,
                        pair TEXT,
                        dex TEXT,
                        requested_size_usd FLOAT,
                        approved_size_usd FLOAT,
                        actual_size_usd FLOAT,
                        expected_profit_usd FLOAT,
                        realized_profit_usd FLOAT,
                        gas_cost_usd FLOAT,
                        net_profit_usd FLOAT,
                        profit_margin_pct FLOAT,
                        slippage_bps FLOAT,
                        gas_used BIGINT,
                        gas_price_gwei FLOAT,
                        sandwiched BOOLEAN DEFAULT FALSE,
                        risk_state JSONB,
                        opportunity_data JSONB,
                        decision_context JSONB,
                        execution_metadata JSONB,
                        error TEXT,
                        CONSTRAINT unique_tx_hash UNIQUE (tx_hash)
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_chain_mode ON trades(chain, mode, created_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_strategy_executed ON trades(strategy, executed, created_at DESC)"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair, created_at DESC)")

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS strategy_performance (
                        id SERIAL PRIMARY KEY,
                        date DATE NOT NULL,
                        family TEXT NOT NULL,
                        chain TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        opportunities_total INT DEFAULT 0,
                        trades_attempted INT DEFAULT 0,
                        trades_executed INT DEFAULT 0,
                        trades_succeeded INT DEFAULT 0,
                        trades_failed INT DEFAULT 0,
                        gross_profit_usd FLOAT DEFAULT 0,
                        gas_cost_usd FLOAT DEFAULT 0,
                        net_profit_usd FLOAT DEFAULT 0,
                        win_rate FLOAT DEFAULT 0,
                        avg_profit_per_trade FLOAT DEFAULT 0,
                        largest_win_usd FLOAT DEFAULT 0,
                        largest_loss_usd FLOAT DEFAULT 0,
                        avg_decision_latency_ms FLOAT DEFAULT 0,
                        avg_execution_latency_ms FLOAT DEFAULT 0,
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(date, family, chain, mode, strategy)
                    )
                    """
                )
        except Exception:
            LOG.exception("trade_recorder_schema_init_failed")
            raise

    def _refresh_schema_cache(self) -> None:
        """Cache current ``trades`` table column names for compatibility inserts."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'trades'
                    """
                )
                rows = cur.fetchall() or []
                self._trade_columns = {str(r["column_name"]) for r in rows if r.get("column_name")}
        except Exception:
            LOG.exception("trade_recorder_schema_cache_failed")
            self._trade_columns = set()

    async def record_trade(
        self,
        opportunity: Dict[str, Any],
        decision: Dict[str, Any],
        execution: "ExecutionResult",
        risk_state: Dict[str, Any],
    ) -> int:
        """Insert one trade record and update strategy aggregate metrics."""
        return await asyncio.to_thread(self._record_trade_sync, opportunity, decision, execution, risk_state)

    def _record_trade_sync(
        self,
        opportunity: Dict[str, Any],
        decision: Dict[str, Any],
        execution: "ExecutionResult",
        risk_state: Dict[str, Any],
    ) -> int:
        try:
            token_in = str(opportunity.get("token_in") or "")
            token_out = str(opportunity.get("token_out") or "")
            pair = f"{token_in}-{token_out}" if token_in or token_out else ""
            execution_metadata = getattr(execution, "metadata", None) or {}

            requested_size = _as_float(opportunity.get("requested_size_usd", opportunity.get("size_usd")), 0.0)
            approved_size = _as_float(
                opportunity.get("approved_size_usd", execution_metadata.get("approved_size_usd", 0.0)),
                0.0,
            )
            actual_size = _as_float(opportunity.get("actual_size_usd", approved_size), approved_size)

            realized_profit = _as_float(execution.realized_profit_usd, 0.0)
            gas_cost = _as_float(execution.gas_cost_usd, 0.0)
            net_profit = realized_profit - gas_cost
            denom = approved_size if approved_size > 0 else 0.0
            profit_margin_pct = ((net_profit / denom) * 100.0) if denom > 0 else 0.0

            decision_latency = _as_float(decision.get("latency_ms", 0.0), 0.0)
            execution_latency = _as_float(execution.latency_ms, 0.0)

            with self._connect() as conn, conn.cursor() as cur:
                column_values: list[tuple[str, Any]] = [
                    ("opportunity_id", str(opportunity.get("id") or opportunity.get("opportunity_id") or "")),
                    ("opportunity_type", str(opportunity.get("type") or opportunity.get("opportunity_type") or "")),
                    ("detector", str(opportunity.get("detector") or "")),
                    ("family", str(opportunity.get("family") or decision.get("family") or "unknown")),
                    ("chain", str(opportunity.get("chain") or decision.get("chain") or "unknown")),
                    ("network", str(opportunity.get("network") or decision.get("network") or "")),
                    ("mode", str(execution.mode or decision.get("mode") or "none")),
                    ("strategy", str(execution.strategy or decision.get("strategy") or "unknown")),
                    ("decision_reason", str(decision.get("reason") or decision.get("decision_reason") or execution.reason or "")),
                    ("decision_latency_ms", decision_latency),
                    ("executed", bool(execution.executed)),
                    ("execution_reason", str(execution.reason or decision.get("execution_reason") or "")),
                    ("execution_latency_ms", execution_latency),
                    ("tx_hash", str(execution.tx_hash or "") or None),
                    ("bundle_tag", str(execution.bundle_tag or "") or None),
                    ("relay", str(opportunity.get("relay") or execution_metadata.get("relay") or "") or None),
                    ("token_in", token_in or None),
                    ("token_out", token_out or None),
                    ("pair", pair or None),
                    ("dex", str(opportunity.get("dex") or "") or None),
                    ("requested_size_usd", requested_size),
                    ("approved_size_usd", approved_size),
                    ("actual_size_usd", actual_size),
                    ("expected_profit_usd", _as_float(execution.expected_profit_usd, 0.0)),
                    ("realized_profit_usd", realized_profit),
                    ("gas_cost_usd", gas_cost),
                    ("net_profit_usd", net_profit),
                    ("profit_margin_pct", profit_margin_pct),
                    ("slippage_bps", _as_float(execution.slippage_bps, 0.0)),
                    ("gas_used", int(opportunity.get("gas_used")) if opportunity.get("gas_used") is not None else None),
                    ("gas_price_gwei", _as_float(opportunity.get("gas_price_gwei"), 0.0)),
                    ("sandwiched", _as_bool(opportunity.get("sandwiched"), False)),
                    ("risk_state", json.dumps(risk_state or {})),
                    ("opportunity_data", json.dumps(opportunity or {})),
                    ("decision_context", json.dumps(decision or {})),
                    ("execution_metadata", json.dumps(execution_metadata)),
                    ("error", str(execution.error or "") or None),
                ]

                # Backward compatibility with older schema variants used in this repo.
                if "status" in self._trade_columns:
                    if execution.executed and not execution.error:
                        legacy_status = "success"
                    elif execution.executed and execution.error:
                        legacy_status = "failed"
                    else:
                        legacy_status = "pending"
                    column_values.append(("status", legacy_status))
                if "reason" in self._trade_columns:
                    column_values.append(("reason", str(execution.reason or decision.get("reason") or "")))
                if "params" in self._trade_columns:
                    column_values.append(("params", json.dumps(opportunity or {})))
                if "slippage" in self._trade_columns:
                    column_values.append(("slippage", _as_float(execution.slippage_bps, 0.0)))
                if "executed_at" in self._trade_columns and execution.executed:
                    column_values.append(("executed_at", "NOW()"))

                present: list[tuple[str, Any]] = []
                for column, value in column_values:
                    if not self._trade_columns or column in self._trade_columns:
                        present.append((column, value))

                cols = ", ".join(col for col, _ in present)
                placeholders = ", ".join(
                    "NOW()" if (col == "executed_at" and val == "NOW()") else ("%s::jsonb" if col in {"risk_state", "opportunity_data", "decision_context", "execution_metadata", "params"} else "%s")
                    for col, val in present
                )
                values = tuple(val for _, val in present if not (isinstance(val, str) and val == "NOW()"))
                # Need separate value tuple when NOW() literal is present.
                if any(col == "executed_at" and val == "NOW()" for col, val in present):
                    values_list: list[Any] = []
                    for col, val in present:
                        if col == "executed_at" and val == "NOW()":
                            continue
                        values_list.append(val)
                    values = tuple(values_list)

                cur.execute(
                    f"INSERT INTO trades ({cols}) VALUES ({placeholders}) RETURNING id",
                    values,
                )
                row = cur.fetchone()
                trade_id = int(row["id"]) if row and row.get("id") is not None else -1

            self._update_strategy_performance(opportunity, decision, execution, net_profit, decision_latency, execution_latency)
            LOG.info(
                "Trade recorded: id=%s mode=%s strategy=%s executed=%s profit=$%.2f",
                trade_id,
                execution.mode,
                execution.strategy,
                execution.executed,
                net_profit,
            )
            return trade_id
        except Exception:
            LOG.exception(
                "trade_record_failed mode=%s strategy=%s opportunity_id=%s",
                getattr(execution, "mode", "unknown"),
                getattr(execution, "strategy", "unknown"),
                opportunity.get("id") or opportunity.get("opportunity_id"),
            )
            return -1

    def _update_strategy_performance(
        self,
        opportunity: Dict[str, Any],
        decision: Dict[str, Any],
        execution: "ExecutionResult",
        net_profit: float,
        decision_latency_ms_value: float,
        execution_latency_ms_value: float,
    ) -> None:
        """Upsert and increment daily strategy aggregates."""
        family = str(opportunity.get("family") or decision.get("family") or "unknown")
        chain = str(opportunity.get("chain") or decision.get("chain") or "unknown")
        mode = str(execution.mode or decision.get("mode") or "none")
        strategy = str(execution.strategy or decision.get("strategy") or "unknown")

        opportunities_total = 1
        trades_attempted = 1
        trades_executed = 1 if execution.executed else 0
        trades_succeeded = 1 if execution.executed and not execution.error and net_profit > 0 else 0
        trades_failed = 1 if execution.executed and trades_succeeded == 0 else 0

        gross_profit = _as_float(execution.realized_profit_usd, 0.0)
        gas_cost = _as_float(execution.gas_cost_usd, 0.0)
        largest_win = net_profit if net_profit > 0 else 0.0
        largest_loss = net_profit if net_profit < 0 else 0.0

        win_rate = (float(trades_succeeded) / float(trades_executed) * 100.0) if trades_executed > 0 else 0.0
        avg_profit_per_trade = (net_profit / float(trades_executed)) if trades_executed > 0 else 0.0
        avg_execution_latency_seed = execution_latency_ms_value if trades_executed > 0 else 0.0

        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO strategy_performance (
                        date, family, chain, mode, strategy,
                        opportunities_total, trades_attempted, trades_executed, trades_succeeded, trades_failed,
                        gross_profit_usd, gas_cost_usd, net_profit_usd,
                        win_rate, avg_profit_per_trade,
                        largest_win_usd, largest_loss_usd,
                        avg_decision_latency_ms, avg_execution_latency_ms,
                        updated_at
                    )
                    VALUES (
                        CURRENT_DATE, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s,
                        NOW()
                    )
                    ON CONFLICT (date, family, chain, mode, strategy)
                    DO UPDATE SET
                        opportunities_total = strategy_performance.opportunities_total + EXCLUDED.opportunities_total,
                        trades_attempted = strategy_performance.trades_attempted + EXCLUDED.trades_attempted,
                        trades_executed = strategy_performance.trades_executed + EXCLUDED.trades_executed,
                        trades_succeeded = strategy_performance.trades_succeeded + EXCLUDED.trades_succeeded,
                        trades_failed = strategy_performance.trades_failed + EXCLUDED.trades_failed,
                        gross_profit_usd = strategy_performance.gross_profit_usd + EXCLUDED.gross_profit_usd,
                        gas_cost_usd = strategy_performance.gas_cost_usd + EXCLUDED.gas_cost_usd,
                        net_profit_usd = strategy_performance.net_profit_usd + EXCLUDED.net_profit_usd,
                        avg_decision_latency_ms = CASE
                            WHEN (strategy_performance.opportunities_total + EXCLUDED.opportunities_total) > 0 THEN
                                ((strategy_performance.avg_decision_latency_ms * strategy_performance.opportunities_total)
                                 + (EXCLUDED.avg_decision_latency_ms * EXCLUDED.opportunities_total))
                                / (strategy_performance.opportunities_total + EXCLUDED.opportunities_total)
                            ELSE 0
                        END,
                        avg_execution_latency_ms = CASE
                            WHEN (strategy_performance.trades_executed + EXCLUDED.trades_executed) > 0 THEN
                                ((strategy_performance.avg_execution_latency_ms * strategy_performance.trades_executed)
                                 + (EXCLUDED.avg_execution_latency_ms * EXCLUDED.trades_executed))
                                / (strategy_performance.trades_executed + EXCLUDED.trades_executed)
                            ELSE 0
                        END,
                        largest_win_usd = GREATEST(strategy_performance.largest_win_usd, EXCLUDED.largest_win_usd),
                        largest_loss_usd = LEAST(strategy_performance.largest_loss_usd, EXCLUDED.largest_loss_usd),
                        win_rate = CASE
                            WHEN (strategy_performance.trades_executed + EXCLUDED.trades_executed) > 0 THEN
                                ((strategy_performance.trades_succeeded + EXCLUDED.trades_succeeded)::FLOAT
                                 / (strategy_performance.trades_executed + EXCLUDED.trades_executed)::FLOAT) * 100.0
                            ELSE 0
                        END,
                        avg_profit_per_trade = CASE
                            WHEN (strategy_performance.trades_executed + EXCLUDED.trades_executed) > 0 THEN
                                (strategy_performance.net_profit_usd + EXCLUDED.net_profit_usd)
                                / (strategy_performance.trades_executed + EXCLUDED.trades_executed)::FLOAT
                            ELSE 0
                        END,
                        updated_at = NOW()
                    """,
                    (
                        family,
                        chain,
                        mode,
                        strategy,
                        opportunities_total,
                        trades_attempted,
                        trades_executed,
                        trades_succeeded,
                        trades_failed,
                        gross_profit,
                        gas_cost,
                        net_profit,
                        win_rate,
                        avg_profit_per_trade,
                        largest_win,
                        largest_loss,
                        decision_latency_ms_value,
                        avg_execution_latency_seed,
                    ),
                )
        except Exception:
            LOG.exception(
                "strategy_performance_update_failed family=%s chain=%s mode=%s strategy=%s",
                family,
                chain,
                mode,
                strategy,
            )

    async def get_recent_trades(
        self,
        limit: int,
        mode: str | None = None,
        strategy: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Return recent trades with optional mode/strategy filters."""
        return await asyncio.to_thread(self._get_recent_trades_sync, limit, mode, strategy)

    def _get_recent_trades_sync(
        self,
        limit: int,
        mode: str | None,
        strategy: str | None,
    ) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        where: List[str] = []
        params: List[Any] = []
        if mode:
            where.append("mode = %s")
            params.append(mode)
        if strategy:
            where.append("strategy = %s")
            params.append(strategy)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT *
            FROM trades
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s
        """
        params.append(safe_limit)

        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall() or []
                return [dict(r) for r in rows]
        except Exception:
            LOG.exception("recent_trades_query_failed mode=%s strategy=%s limit=%s", mode, strategy, safe_limit)
            return []

    async def get_strategy_performance(
        self,
        days: int,
        strategy: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Return strategy performance rows for the requested lookback window."""
        return await asyncio.to_thread(self._get_strategy_performance_sync, days, strategy)

    def _get_strategy_performance_sync(self, days: int, strategy: str | None) -> List[Dict[str, Any]]:
        safe_days = max(1, min(int(days), 3650))
        params: List[Any] = [safe_days]
        where = ""
        if strategy:
            where = "AND strategy = %s"
            params.append(strategy)

        query = f"""
            SELECT *
            FROM strategy_performance
            WHERE date >= (CURRENT_DATE - (%s * INTERVAL '1 day'))
              {where}
            ORDER BY date DESC, family, chain, mode, strategy
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall() or []
                return [dict(r) for r in rows]
        except Exception:
            LOG.exception("strategy_performance_query_failed days=%s strategy=%s", safe_days, strategy)
            return []

    async def get_pnl_summary(self, start_date: date) -> Dict[str, Any]:
        """Return aggregate PnL summary from ``start_date`` (inclusive)."""
        return await asyncio.to_thread(self._get_pnl_summary_sync, start_date)

    def _get_pnl_summary_sync(self, start_date_value: date) -> Dict[str, Any]:
        query = """
            SELECT
                COALESCE(SUM(expected_profit_usd), 0) AS expected_profit_usd,
                COALESCE(SUM(realized_profit_usd), 0) AS realized_profit_usd,
                COALESCE(SUM(gas_cost_usd), 0) AS gas_cost_usd,
                COALESCE(SUM(net_profit_usd), 0) AS net_profit_usd,
                COUNT(*) AS total_records,
                COALESCE(SUM(CASE WHEN executed THEN 1 ELSE 0 END), 0) AS executed_records,
                COALESCE(SUM(CASE WHEN executed AND net_profit_usd > 0 THEN 1 ELSE 0 END), 0) AS winning_trades,
                COALESCE(SUM(CASE WHEN executed AND net_profit_usd <= 0 THEN 1 ELSE 0 END), 0) AS losing_trades
            FROM trades
            WHERE created_at::date >= %s
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(query, (start_date_value,))
                row = cur.fetchone() or {}
                return dict(row)
        except Exception:
            LOG.exception("pnl_summary_query_failed start_date=%s", start_date_value)
            return {
                "expected_profit_usd": 0.0,
                "realized_profit_usd": 0.0,
                "gas_cost_usd": 0.0,
                "net_profit_usd": 0.0,
                "total_records": 0,
                "executed_records": 0,
                "winning_trades": 0,
                "losing_trades": 0,
            }

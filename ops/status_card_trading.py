from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("status-card-trading")


@dataclass
class TradingStats:
    trades_today: int = 0
    trades_last_hour: int = 0
    win_rate_today: float = 0.0
    net_pnl_today: float = 0.0
    active_strategies: str = ""
    last_trade_time: Optional[str] = None
    last_trade_profit: float = 0.0
    opportunities_seen_hour: int = 0


def fetch_trading_stats(database_url: str) -> TradingStats:
    """Fetch live trading aggregates for status-card display.

    Returns default/empty values when the database is unavailable or queries fail.
    """
    try:
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN executed AND net_profit_usd > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(net_profit_usd) AS net_pnl
                FROM trades
                WHERE created_at >= CURRENT_DATE
                """
            )
            today = cur.fetchone() or {}
            total_today = int(today.get("total") or 0)
            wins_today = int(today.get("wins") or 0)
            net_pnl_today = float(today.get("net_pnl") or 0.0)

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE created_at >= NOW() - INTERVAL '1 hour'
                  AND executed = true
                """
            )
            last_hour = cur.fetchone() or {}
            trades_last_hour = int(last_hour.get("total") or 0)

            cur.execute(
                """
                SELECT created_at, net_profit_usd
                FROM trades
                WHERE executed = true
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            last_trade = cur.fetchone() or {}

            cur.execute(
                """
                SELECT DISTINCT strategy
                FROM trades
                WHERE created_at >= NOW() - INTERVAL '1 hour'
                  AND executed = true
                ORDER BY strategy
                """
            )
            strategy_rows = cur.fetchall() or []
            strategies = [str(r.get("strategy") or "").strip() for r in strategy_rows]
            strategies = [s for s in strategies if s]
            active_strategies = ", ".join(strategies)
            if len(active_strategies) > 50:
                active_strategies = f"{active_strategies[:47]}..."

            # Proxy for "opportunities seen" based on recorded trade decisions in the last hour.
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE created_at >= NOW() - INTERVAL '1 hour'
                """
            )
            opp_seen = cur.fetchone() or {}
            opportunities_seen_hour = int(opp_seen.get("total") or 0)

            win_rate_today = (float(wins_today) / float(total_today) * 100.0) if total_today > 0 else 0.0

            last_created_at = last_trade.get("created_at")
            if isinstance(last_created_at, datetime):
                last_trade_time = last_created_at.astimezone(timezone.utc).isoformat()
            else:
                last_trade_time = None

            return TradingStats(
                trades_today=total_today,
                trades_last_hour=trades_last_hour,
                win_rate_today=win_rate_today,
                net_pnl_today=net_pnl_today,
                active_strategies=active_strategies,
                last_trade_time=last_trade_time,
                last_trade_profit=float(last_trade.get("net_profit_usd") or 0.0),
                opportunities_seen_hour=opportunities_seen_hour,
            )
    except Exception:
        log.exception("fetch_trading_stats_failed", exc_info=True)
        return TradingStats()


def format_trading_section(stats: TradingStats) -> str:
    """Format trading stats as a Discord markdown section."""
    pnl_icon = "🟢" if stats.net_pnl_today >= 0 else "🔴"
    last_icon = "🟢" if stats.last_trade_profit >= 0 else "🔴"
    last_trade_time = stats.last_trade_time or "—"
    strategies = stats.active_strategies or "—"
    return (
        "**Trading Stats**\n"
        f"• Trades Today: **{stats.trades_today}** | Last Hour: **{stats.trades_last_hour}**\n"
        f"• Win Rate Today: **{stats.win_rate_today:.1f}%**\n"
        f"• Net P&L Today: {pnl_icon} **${stats.net_pnl_today:,.2f}**\n"
        f"• Last Trade: {last_icon} **${stats.last_trade_profit:,.2f}** at `{last_trade_time}`\n"
        f"• Opportunities Seen (1h): **{stats.opportunities_seen_hour}**\n"
        f"• Active Strategies (1h): `{strategies}`"
    )

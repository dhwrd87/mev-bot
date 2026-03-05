from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import discord
import psycopg
from discord import app_commands
from discord.ext import commands
from psycopg.rows import dict_row

log = logging.getLogger("discord-trading-commands")


def _short_tx(tx_hash: str | None) -> str:
    s = str(tx_hash or "").strip()
    if not s:
        return "—"
    if len(s) <= 14:
        return s
    return f"{s[:10]}...{s[-4:]}"


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"


def _num(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


class TradingCommands(commands.Cog):
    """Discord slash commands for trading decisions, trades, and P&L visibility."""

    def __init__(self, bot: commands.Bot, database_url: str) -> None:
        self.bot = bot
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    @staticmethod
    def _format_time_ago(delta: timedelta) -> str:
        total = int(max(0.0, delta.total_seconds()))
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m"
        if total < 86400:
            return f"{total // 3600}h"
        return f"{total // 86400}d"

    async def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall() or []
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_run)

    async def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row else {}

        return await asyncio.to_thread(_run)

    @app_commands.command(name="trades", description="Show recent recorded trades")
    async def trades(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 100] = 10,
        mode: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            where = []
            params: list[Any] = []
            if mode:
                where.append("mode = %s")
                params.append(mode)
            if strategy:
                where.append("strategy = %s")
                params.append(strategy)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""

            total_row = await self._fetchone(f"SELECT COUNT(*) AS c FROM trades {where_sql}", tuple(params))
            total_count = int(total_row.get("c", 0) or 0)

            q = f"""
                SELECT id, created_at, mode, strategy, executed, pair, dex,
                       approved_size_usd, expected_profit_usd, realized_profit_usd,
                       gas_cost_usd, net_profit_usd, tx_hash, execution_reason
                FROM trades
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s
            """
            params.append(int(limit))
            rows = await self._fetchall(q, tuple(params))

            em = discord.Embed(
                title=f"📊 Recent Trades ({len(rows)})",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow(),
            )
            now = datetime.utcnow()
            for r in rows[:10]:
                executed = bool(r.get("executed"))
                icon = "✅" if executed else "❌"
                net = _num(r.get("net_profit_usd"))
                pnl_icon = "🟢" if net >= 0 else "🔴"
                created = r.get("created_at")
                if isinstance(created, datetime):
                    ago = self._format_time_ago(now - created.replace(tzinfo=None))
                else:
                    ago = "?"
                em.add_field(
                    name=f"{icon} Trade #{r.get('id')} - {r.get('mode')}/{r.get('strategy')}",
                    value=(
                        f"**Pair:** {r.get('pair') or '—'} | **DEX:** {r.get('dex') or '—'}\n"
                        f"**Size:** {_money(r.get('approved_size_usd'))} | **Expected:** {_money(r.get('expected_profit_usd'))}\n"
                        f"**Realized:** {pnl_icon} {_money(r.get('realized_profit_usd'))} | **Gas:** {_money(r.get('gas_cost_usd'))}\n"
                        f"**Tx:** `{_short_tx(r.get('tx_hash'))}` | **Ago:** {ago}"
                    ),
                    inline=False,
                )
            em.set_footer(text=f"Showing {min(len(rows), 10)} of {total_count} trades")
            await interaction.followup.send(embed=em)
        except Exception:
            log.exception("trades_command_failed", exc_info=True)
            await interaction.followup.send("Failed to fetch trades. Please try again.", ephemeral=True)

    @app_commands.command(name="strategy", description="Show strategy performance rollup")
    async def strategy(
        self,
        interaction: discord.Interaction,
        strategy: Optional[str] = None,
        days: app_commands.Range[int, 1, 365] = 7,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            where_strategy = ""
            params: list[Any] = [int(days)]
            if strategy:
                where_strategy = "AND strategy = %s"
                params.append(strategy)

            q = f"""
                SELECT strategy, mode,
                       SUM(opportunities_total) AS total_opps,
                       SUM(trades_attempted) AS attempts,
                       SUM(trades_executed) AS executions,
                       SUM(trades_succeeded) AS wins,
                       SUM(trades_failed) AS losses,
                       SUM(net_profit_usd) AS net_profit,
                       SUM(gas_cost_usd) AS total_gas,
                       AVG(win_rate) AS avg_win_rate,
                       AVG(avg_profit_per_trade) AS avg_profit
                FROM strategy_performance
                WHERE date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                  {where_strategy}
                GROUP BY strategy, mode
                ORDER BY net_profit DESC NULLS LAST
            """
            rows = await self._fetchall(q, tuple(params))

            em = discord.Embed(
                title=f"📈 Strategy Performance (Last {days} days)",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow(),
            )
            for r in rows[:10]:
                net = _num(r.get("net_profit"))
                pnl_icon = "🟢" if net >= 0 else "🔴"
                em.add_field(
                    name=f"{r.get('strategy')} ({r.get('mode')})",
                    value=(
                        f"**Opportunities:** {int(r.get('total_opps') or 0)}\n"
                        f"**Executions/Attempts:** {int(r.get('executions') or 0)}/{int(r.get('attempts') or 0)}\n"
                        f"**Win Rate:** {_num(r.get('avg_win_rate')):.1f}% | **W/L:** {int(r.get('wins') or 0)}/{int(r.get('losses') or 0)}\n"
                        f"**Net P&L:** {pnl_icon} {_money(net)} | **Avg Profit:** {_money(r.get('avg_profit'))}\n"
                        f"**Total Gas:** {_money(r.get('total_gas'))}"
                    ),
                    inline=False,
                )
            if not rows:
                em.description = "No strategy performance rows found for this period."
            await interaction.followup.send(embed=em)
        except Exception:
            log.exception("strategy_command_failed", exc_info=True)
            await interaction.followup.send("Failed to fetch strategy performance. Please try again.", ephemeral=True)

    @app_commands.command(name="pnl", description="Show P&L summary by period")
    @app_commands.describe(period="today, yesterday, week, or month")
    async def pnl(self, interaction: discord.Interaction, period: str = "today") -> None:
        await interaction.response.defer(thinking=True)
        try:
            p = str(period).strip().lower()
            now = datetime.utcnow()
            if p == "today":
                start = datetime(now.year, now.month, now.day)
            elif p == "yesterday":
                start = datetime(now.year, now.month, now.day) - timedelta(days=1)
            elif p == "week":
                start = now - timedelta(days=7)
            elif p == "month":
                start = now - timedelta(days=30)
            else:
                await interaction.followup.send("Invalid period. Use: today, yesterday, week, month.", ephemeral=True)
                return

            overall_q = """
                SELECT COUNT(*) AS total_trades,
                       SUM(CASE WHEN executed THEN 1 ELSE 0 END) AS executed,
                       SUM(CASE WHEN executed AND net_profit_usd > 0 THEN 1 ELSE 0 END) AS profitable,
                       SUM(realized_profit_usd) AS gross_profit,
                       SUM(gas_cost_usd) AS total_gas,
                       SUM(net_profit_usd) AS net_profit,
                       MAX(net_profit_usd) AS best_trade,
                       MIN(net_profit_usd) AS worst_trade,
                       AVG(net_profit_usd) AS avg_profit
                FROM trades
                WHERE created_at >= %s
            """
            by_mode_q = """
                SELECT mode, COUNT(*) AS trades, SUM(net_profit_usd) AS net_profit
                FROM trades
                WHERE created_at >= %s AND executed = true
                GROUP BY mode
            """
            top_strategy_q = """
                SELECT strategy, COUNT(*) AS trades, SUM(net_profit_usd) AS net_profit
                FROM trades
                WHERE created_at >= %s AND executed = true
                GROUP BY strategy
                ORDER BY net_profit DESC NULLS LAST
                LIMIT 5
            """

            overall = await self._fetchone(overall_q, (start,))
            by_mode = await self._fetchall(by_mode_q, (start,))
            top_strats = await self._fetchall(top_strategy_q, (start,))

            net = _num(overall.get("net_profit"))
            color = discord.Color.green() if net > 0 else discord.Color.red()
            pnl_icon = "🟢" if net > 0 else "🔴"
            executed = int(overall.get("executed") or 0)
            profitable = int(overall.get("profitable") or 0)
            win_rate = (profitable / executed * 100.0) if executed > 0 else 0.0

            em = discord.Embed(
                title=f"💰 P&L Summary - {p}",
                color=color,
                timestamp=datetime.utcnow(),
            )
            em.add_field(
                name="Overall",
                value=(
                    f"**Net P&L:** {pnl_icon} {_money(net)}\n"
                    f"**Gross:** {_money(overall.get('gross_profit'))} | **Gas:** {_money(overall.get('total_gas'))}\n"
                    f"**Trades:** {int(overall.get('total_trades') or 0)} total / {executed} executed\n"
                    f"**Win Rate:** {win_rate:.1f}% | **Avg Profit:** {_money(overall.get('avg_profit'))}\n"
                    f"**Best/Worst:** {_money(overall.get('best_trade'))} / {_money(overall.get('worst_trade'))}"
                ),
                inline=False,
            )

            if by_mode:
                lines = []
                for r in by_mode:
                    m_net = _num(r.get("net_profit"))
                    icon = "🟢" if m_net >= 0 else "🔴"
                    lines.append(f"{icon} **{r.get('mode')}**: {_money(m_net)} ({int(r.get('trades') or 0)} trades)")
                em.add_field(name="By Mode", value="\n".join(lines), inline=False)

            if top_strats:
                lines = []
                for r in top_strats:
                    s_net = _num(r.get("net_profit"))
                    icon = "🟢" if s_net >= 0 else "🔴"
                    lines.append(f"{icon} **{r.get('strategy')}**: {_money(s_net)} ({int(r.get('trades') or 0)} trades)")
                em.add_field(name="Top Strategies", value="\n".join(lines), inline=False)

            await interaction.followup.send(embed=em)
        except Exception:
            log.exception("pnl_command_failed", exc_info=True)
            await interaction.followup.send("Failed to fetch P&L summary. Please try again.", ephemeral=True)

    @app_commands.command(name="decisions", description="Show recent trading decisions")
    async def decisions(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 15,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            q = """
                SELECT id, created_at, opportunity_type, mode, strategy,
                       decision_reason, executed, execution_reason,
                       expected_profit_usd, net_profit_usd, approved_size_usd
                FROM trades
                ORDER BY created_at DESC
                LIMIT %s
            """
            rows = await self._fetchall(q, (int(limit),))

            em = discord.Embed(
                title="🧠 Recent Trading Decisions",
                color=discord.Color.purple(),
                timestamp=datetime.utcnow(),
            )
            now = datetime.utcnow()
            for r in rows[:15]:
                executed = bool(r.get("executed"))
                icon = "✅" if executed else "⏸️"
                created = r.get("created_at")
                if isinstance(created, datetime):
                    ago = self._format_time_ago(now - created.replace(tzinfo=None))
                else:
                    ago = "?"
                if executed:
                    result = f"Result: {_money(r.get('net_profit_usd'))}"
                else:
                    result = f"Not Executed: {r.get('execution_reason') or '—'}"
                em.add_field(
                    name=f"{icon} #{r.get('id')} - {r.get('opportunity_type') or 'unknown'}",
                    value=(
                        f"Decision: **{r.get('mode')}/{r.get('strategy')}**\n"
                        f"Reason: `{r.get('decision_reason') or '—'}`\n"
                        f"Size: {_money(r.get('approved_size_usd'))} | Expected: {_money(r.get('expected_profit_usd'))}\n"
                        f"{result} | {ago} ago"
                    ),
                    inline=False,
                )
            if not rows:
                em.description = "No recent decisions found."
            await interaction.followup.send(embed=em)
        except Exception:
            log.exception("decisions_command_failed", exc_info=True)
            await interaction.followup.send("Failed to fetch decisions. Please try again.", ephemeral=True)


async def setup(bot: commands.Bot, database_url: str | None = None) -> None:
    resolved_db_url = str(database_url or getattr(bot, "database_url", "") or "").strip()
    if not resolved_db_url:
        resolved_db_url = str(os.environ.get("DATABASE_URL", "")).strip()
    await bot.add_cog(TradingCommands(bot=bot, database_url=resolved_db_url))

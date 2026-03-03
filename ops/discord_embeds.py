from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import discord


def _safe(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).strip()
    return s if s else "—"


def build_status_embed(snapshot: Dict[str, Any]) -> discord.Embed:
    em = discord.Embed(
        title="MEV Bot Status",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Bot State", value=_safe(snapshot.get("bot_state")), inline=True)
    em.add_field(name="Mode", value=_safe(snapshot.get("mode")), inline=True)
    em.add_field(name="Active Chain", value=_safe(snapshot.get("active_chain")), inline=True)
    em.add_field(name="Snapshot", value=_safe(snapshot.get("snapshot_status")), inline=True)
    em.add_field(name="Last Heartbeat (UTC)", value=_safe(snapshot.get("heartbeat_utc")), inline=False)
    em.add_field(name="Head/Slot/Lag", value=_safe(snapshot.get("head_slot_lag")), inline=False)
    em.add_field(name="Process Uptime", value=_safe(snapshot.get("uptime")), inline=True)
    em.add_field(name="Error Counters", value=_safe(snapshot.get("error_counters")), inline=True)
    em.add_field(name="Latest Trade", value=_safe(snapshot.get("latest_trade")), inline=False)
    em.add_field(name="RPC Latency p95", value=_safe(snapshot.get("rpc_p95")), inline=True)
    em.add_field(name="RPC Latency p99", value=_safe(snapshot.get("rpc_p99")), inline=True)
    em.add_field(name="Confirm Latency p95", value=_safe(snapshot.get("confirm_p95")), inline=True)
    em.add_field(name="Trades (10m) sent/failed", value=_safe(snapshot.get("trades_10m")), inline=True)
    em.add_field(name="Opportunities (10m) seen/attempted/filled", value=_safe(snapshot.get("opportunities_10m")), inline=False)
    em.add_field(name="DEX Health", value=_safe(snapshot.get("dex_health")), inline=False)
    em.add_field(name="PnL / Drawdown / Fees", value=_safe(snapshot.get("pnl_drawdown_fees")), inline=False)
    em.set_footer(text="Unavailable values are shown as —")
    return em


def build_audit_embed(payload: Dict[str, Any]) -> discord.Embed:
    ok = bool(payload.get("ok", False))
    em = discord.Embed(
        title="Operator Audit",
        color=discord.Color.green() if ok else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Timestamp (UTC ISO)", value=_safe(payload.get("ts_utc")), inline=False)
    em.add_field(name="Actor", value=_safe(payload.get("actor")), inline=False)
    em.add_field(name="Command", value=_safe(payload.get("command")), inline=False)
    em.add_field(name="Result", value=_safe(payload.get("result")), inline=False)
    return em

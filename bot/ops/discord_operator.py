from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any
from urllib.parse import urlencode

import httpx
import psycopg
from discord.ext import commands
import discord
from discord import app_commands
from psycopg import errors as pg_errors

from bot.core.chain_adapter import parse_chain_selection
from bot.ops.status_card import StatusCardManager, StatusCardSnapshot, fmt_num


log = logging.getLogger("discord-operator")


def _ops_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        return dsn
    user = os.getenv("POSTGRES_USER", "mev_user")
    pwd = os.getenv("POSTGRES_PASSWORD", "change_me")
    db = os.getenv("POSTGRES_DB", "mev_bot")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def _ensure_ops_state_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_state(
          k TEXT PRIMARY KEY,
          v TEXT NOT NULL,
          updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute("INSERT INTO ops_state(k, v) VALUES ('paused', 'true') ON CONFLICT (k) DO NOTHING")
    conn.execute("INSERT INTO ops_state(k, v) VALUES ('mode', 'paper') ON CONFLICT (k) DO NOTHING")
    conn.execute("INSERT INTO ops_state(k, v) VALUES ('kill_switch', 'true') ON CONFLICT (k) DO NOTHING")
    default_chain = str(os.getenv("CHAIN", "sepolia")).strip().lower() or "sepolia"
    conn.execute(
        "INSERT INTO ops_state(k, v) VALUES ('chain_selection', %s) ON CONFLICT (k) DO NOTHING",
        (f"EVM:{default_chain}",),
    )


def _ensure_operator_events_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_events(
          op_id TEXT PRIMARY KEY,
          ts TIMESTAMPTZ DEFAULT now(),
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          value TEXT,
          reason TEXT,
          applied BOOLEAN NOT NULL DEFAULT false,
          error TEXT,
          desired_state TEXT,
          desired_mode TEXT,
          desired_chain TEXT,
          effective_state TEXT,
          effective_chain TEXT,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )


def _read_ops_value(key: str, default: str) -> str:
    with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
        _ensure_ops_state_table(conn)
        row = conn.execute("SELECT v FROM ops_state WHERE k=%s", (key,)).fetchone()
        if not row:
            return default
        return str(row[0])


def _write_ops_value(key: str, value: str) -> None:
    with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
        _ensure_ops_state_table(conn)
        conn.execute(
            """
            INSERT INTO ops_state(k, v, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
            """,
            (key, value),
        )


def _to_bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _sum_prom_metric(metrics_text: str, metric_name: str) -> float:
    total = 0.0
    for raw in metrics_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(metric_name):
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                total += float(parts[-1])
            except Exception:
                continue
    return total


@dataclass
class PendingConfirmation:
    user_id: int
    action: str
    value: str
    reason: str
    created_ts: float
    expires_ts: float


class OperatorBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=os.getenv("DISCORD_OPERATOR_PREFIX", "!"), intents=intents)
        self.api_base = os.getenv("MEVBOT_API_URL", os.getenv("DISCORD_OPERATOR_API_BASE", "http://mev-bot:8000")).rstrip("/")
        self.guild_id = int(os.getenv("DISCORD_OPERATOR_GUILD_ID", "0") or "0")
        self.audit_channel_id = int(os.getenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "0") or "0")
        self.command_channel_id = int(os.getenv("DISCORD_OPERATOR_COMMAND_CHANNEL_ID", "0") or "0")
        self.confirm_ttl_s = int(os.getenv("DISCORD_OPERATOR_CONFIRM_TTL_S", "120"))
        self.status_channel_id = int(os.getenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", "0") or "0")
        self.status_refresh_s = int(os.getenv("DISCORD_OPERATOR_STATUS_REFRESH_S", "45"))
        self.instance_id = os.getenv("BOT_INSTANCE_ID", secrets.token_hex(4))
        self.operator_impl = "bot.ops.discord_operator"
        self.sync_mode = "global"
        self.pending: Dict[str, PendingConfirmation] = {}
        self.api_http = httpx.AsyncClient(timeout=8.0)
        self.status_card = StatusCardManager(
            bot=self,
            status_channel_id=self.status_channel_id,
            refresh_s=self.status_refresh_s,
            snapshot_fetcher=self._status_card_snapshot,
            kv_read=_read_ops_value,
            kv_write=_write_ops_value,
            audit_fn=self._audit,
        )

    async def setup_hook(self) -> None:
        try:
            if self.guild_id > 0:
                guild_obj = discord.Object(id=self.guild_id)
                synced = await self.tree.sync(guild=guild_obj)
                self.sync_mode = "guild"
                log.info(
                    "slash commands synced to guild id=%s count=%s operator_impl=%s instance_id=%s",
                    self.guild_id,
                    len(synced),
                    self.operator_impl,
                    self.instance_id,
                )
            else:
                synced = await self.tree.sync()
                self.sync_mode = "global"
                log.info(
                    "slash commands synced globally count=%s operator_impl=%s instance_id=%s",
                    len(synced),
                    self.operator_impl,
                    self.instance_id,
                )
        except Exception as e:
            log.warning("slash command sync failed: %s", e)

    async def close(self) -> None:
        await self.status_card.stop()
        await self.api_http.aclose()
        await super().close()

    async def on_ready(self):
        log.info(
            "discord-operator ready user=%s command_channel_id=%s status_channel_id=%s refresh_s=%s operator_impl=%s instance_id=%s",
            self.user,
            self.command_channel_id,
            self.status_channel_id,
            self.status_refresh_s,
            self.operator_impl,
            self.instance_id,
        )
        self.status_card.start()

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        base = f"{self.api_base}{clean}"
        if params:
            return f"{base}?{urlencode(params, doseq=True)}"
        return base

    async def _check_channel(self, ctx: commands.Context) -> bool:
        if self.command_channel_id and ctx.channel and ctx.channel.id != self.command_channel_id:
            await ctx.reply("Operator commands are restricted to the configured command channel.")
            return False
        return True

    async def _audit(self, text: str) -> None:
        if not self.audit_channel_id:
            log.info("audit(no_channel): %s", text)
            return
        ch = self.get_channel(self.audit_channel_id)
        if not ch:
            try:
                ch = await self.fetch_channel(self.audit_channel_id)
            except Exception:
                log.warning("failed to resolve audit channel id=%s", self.audit_channel_id)
                return
        try:
            await ch.send(text)
        except Exception as e:
            log.warning("failed to send audit message: %s", e)

    async def _api_get(self, path: str, params: Dict[str, Any] | None = None) -> dict:
        r = await self.api_http.get(self._build_url(path, params=params))
        r.raise_for_status()
        return r.json()

    async def _api_post(self, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        r = await self.api_http.post(self._build_url(path), params=params, json=json)
        r.raise_for_status()
        return r.json()

    def _read_desired_fields(self) -> tuple[str, str, str]:
        paused = _to_bool(_read_ops_value("paused", "true"))
        desired_state = "PAUSED" if paused else "TRADING"
        desired_mode = _read_ops_value("mode", "paper")
        desired_chain = _read_ops_value("chain_selection", f"EVM:{str(os.getenv('CHAIN', 'sepolia')).strip().lower() or 'sepolia'}")
        return desired_state, desired_mode, desired_chain

    async def _read_effective_fields(self) -> tuple[str, str]:
        try:
            health = await self._api_get("/health")
            return str(health.get("state", "UNKNOWN")), str(health.get("chain", "unknown"))
        except Exception:
            return "UNKNOWN", "unknown"

    def _record_operator_event_start(self, *, op_id: str, actor: str, action: str, value: str, reason: str) -> None:
        desired_state, desired_mode, desired_chain = self._read_desired_fields()
        with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
            _ensure_ops_state_table(conn)
            _ensure_operator_events_table(conn)
            conn.execute(
                """
                INSERT INTO operator_events(
                  op_id, actor, action, value, reason, applied,
                  desired_state, desired_mode, desired_chain
                )
                VALUES (%s, %s, %s, %s, %s, false, %s, %s, %s)
                ON CONFLICT (op_id) DO NOTHING
                """,
                (op_id, actor, action, value, reason, desired_state, desired_mode, desired_chain),
            )

    def _record_operator_event_finish(
        self,
        *,
        op_id: str,
        applied: bool,
        error: Optional[str],
        effective_state: str,
        effective_chain: str,
    ) -> None:
        desired_state, desired_mode, desired_chain = self._read_desired_fields()
        with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
            _ensure_ops_state_table(conn)
            _ensure_operator_events_table(conn)
            conn.execute(
                """
                UPDATE operator_events
                SET
                  applied=%s,
                  error=%s,
                  desired_state=%s,
                  desired_mode=%s,
                  desired_chain=%s,
                  effective_state=%s,
                  effective_chain=%s,
                  ts=now(),
                  created_at=now()
                WHERE op_id=%s
                """,
                (
                    bool(applied),
                    error,
                    desired_state,
                    desired_mode,
                    desired_chain,
                    effective_state,
                    effective_chain,
                    op_id,
                ),
            )

    async def _set_state(self, target: str, *, actor: str, reason: str, force: bool = False) -> dict:
        return await self._api_post(
            f"/state/{target}",
            params={
                "actor": actor,
                "reason": reason,
                "force": str(bool(force)).lower(),
            },
        )

    async def _status_payload(self) -> Tuple[str, dict]:
        health = await self._api_get("/health")
        status = {}
        with contextlib.suppress(Exception):
            status = await self._api_get("/status")
        metrics_resp = await self.api_http.get(f"{self.api_base}/metrics")
        metrics_resp.raise_for_status()
        metrics_text = metrics_resp.text
        mode = _read_ops_value("mode", "paper")
        kill_switch = _to_bool(_read_ops_value("kill_switch", "true"))
        last_trade_ts, today_pnl = self._trade_stats()
        err_n = _sum_prom_metric(metrics_text, "mevbot_mempool_stream_consume_errors_total")
        ok_n = _sum_prom_metric(metrics_text, "mevbot_mempool_stream_consume_total")
        err_rate_pct = (100.0 * err_n / max(1.0, ok_n))
        rpc_429_ratio = _sum_prom_metric(metrics_text, "mevbot_rpc_429_ratio")
        rpc_circuit_open = _sum_prom_metric(metrics_text, "mevbot_rpc_circuit_breaker_open")
        rpc_health = (
            f"w3_connected={bool(health.get('w3_connected', False))} "
            f"circuit_open={rpc_circuit_open:.0f} 429_ratio={rpc_429_ratio:.3f}"
        )
        chain_sel_raw = _read_ops_value("chain_selection", f"EVM:{health.get('chain', 'unknown')}")
        try:
            chain_sel = parse_chain_selection(chain_sel_raw)
            chain_family = chain_sel.family.lower()
            chain_name = chain_sel.chain
        except Exception:
            chain_family = str(health.get("chain_family", "evm")).lower()
            chain_name = str(health.get("chain", "unknown"))

        payload = {
            "state": str(status.get("effective_state", health.get("state", "UNKNOWN"))),
            "chain": chain_name,
            "paused": bool(health.get("paused", True)),
            "chain_family": chain_family,
            "mode": mode,
            "kill_switch": kill_switch,
            "xlen": _sum_prom_metric(metrics_text, "mevbot_mempool_stream_xlen"),
            "lag": _sum_prom_metric(metrics_text, "mevbot_mempool_stream_group_lag"),
            "rpc_429_total": _sum_prom_metric(metrics_text, "mevbot_rpc_gettx_429_total"),
            "rpc_health": rpc_health,
            "last_trade_time": last_trade_ts or "n/a",
            "today_pnl": today_pnl,
            "error_rate_pct": err_rate_pct,
            "last_op_id_applied": status.get("last_op_id_applied"),
            "last_op_apply_error": status.get("last_op_apply_error"),
            "desired_state": status.get("desired_state"),
            "desired_mode": status.get("desired_mode"),
            "desired_chain": status.get("desired_chain"),
            "effective_chain": status.get("effective_chain", chain_name),
            "rpc_url": status.get("rpc_url", "—"),
            "ws_url": status.get("ws_url", "—"),
            "head": status.get("head"),
            "lag_blocks": status.get("lag_blocks"),
            "switching_in_progress": bool(status.get("switching_in_progress", False)),
            "last_transition_error": status.get("last_transition_error"),
            "mempool_stream": status.get("mempool_stream", "—"),
        }
        msg = (
            f"state={payload['state']} desired_chain={payload['desired_chain'] or '—'} "
            f"effective_chain={payload['effective_chain'] or '—'} paused={payload['paused']} "
            f"mode={payload['mode']} desired_mode={payload['desired_mode'] or '—'} kill_switch={payload['kill_switch']} "
            f"rpc=({payload['rpc_health']}) "
            f"rpc_url={payload['rpc_url'] or '—'} ws_url={payload['ws_url'] or '—'} "
            f"head={payload['head']} lag_blocks={payload['lag_blocks']} switching={payload['switching_in_progress']} "
            f"switch_err={payload['last_transition_error'] or '—'} "
            f"stream={payload['mempool_stream']} "
            f"last_trade={payload['last_trade_time']} today_pnl={payload['today_pnl']} "
            f"error_rate={payload['error_rate_pct']:.2f}% "
            f"xlen={payload['xlen']:.0f} lag={payload['lag']:.0f} rpc429={payload['rpc_429_total']:.0f} "
            f"last_op_id_applied={payload['last_op_id_applied'] or '—'} "
            f"last_op_apply_error={payload['last_op_apply_error'] or '—'}"
        )
        return msg, payload

    def _trade_stats(self) -> tuple[Optional[str], str]:
        q = """
        SELECT
          (SELECT max(COALESCE(ts, created_at)) FROM trades) AS last_trade_ts,
          (SELECT sum(COALESCE(realized_pnl_usd, realized_profit_usd, 0))::double precision
             FROM trades
            WHERE COALESCE(ts, created_at) >= date_trunc('day', now())) AS pnl_today
        """
        try:
            with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
                row = conn.execute(q).fetchone()
                last_trade_ts = row[0].isoformat() if row and row[0] else None
                pnl = float(row[1]) if row and row[1] is not None else None
                return last_trade_ts, ("n/a" if pnl is None else f"${fmt_num(pnl, 2)}")
        except pg_errors.UndefinedTable:
            return None, "n/a"
        except Exception:
            return None, "n/a"

    async def _status_card_snapshot(self) -> StatusCardSnapshot:
        _, payload = await self._status_payload()
        return StatusCardSnapshot(
            state=f"{payload.get('state', 'UNKNOWN')} (desired={payload.get('desired_state', '—')})",
            mode=f"{payload.get('mode', 'paper')} (desired={payload.get('desired_mode', '—')})",
            chain_family=str(payload.get("chain_family", "evm")),
            chain=f"{payload.get('effective_chain', 'unknown')} (desired={payload.get('desired_chain', '—')})",
            rpc_health=(
                f"{payload.get('rpc_health', 'n/a')} "
                f"rpc={payload.get('rpc_url', '—')} ws={payload.get('ws_url', '—')} "
                f"head={payload.get('head', '—')} lag={payload.get('lag_blocks', '—')} "
                f"switching={payload.get('switching_in_progress', False)}"
            ),
            last_trade_time=(
                f"{payload.get('last_trade_time', 'n/a')} "
                f"(switch_err={payload.get('last_transition_error') or '—'})"
            ),
            today_pnl=str(payload.get("today_pnl", "n/a")),
            error_rate=f"{float(payload.get('error_rate_pct', 0.0)):.2f}%",
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

    def _is_dangerous(self, action: str, value: str) -> bool:
        v = value.strip().lower()
        if action == "mode" and v == "live":
            return True
        if action == "kill_switch" and v in {"off", "false", "0"}:
            return True
        return False

    def _new_confirm(self, user_id: int, action: str, value: str, reason: str) -> str:
        code = secrets.token_hex(3).upper()
        now = time.time()
        self.pending[code] = PendingConfirmation(
            user_id=user_id,
            action=action,
            value=value,
            reason=reason,
            created_ts=now,
            expires_ts=now + self.confirm_ttl_s,
        )
        return code

    def _take_confirm(self, user_id: int, code: str) -> Optional[PendingConfirmation]:
        item = self.pending.get(code)
        if not item:
            return None
        if item.user_id != user_id:
            return None
        if time.time() > item.expires_ts:
            self.pending.pop(code, None)
            return None
        self.pending.pop(code, None)
        return item

    async def _apply_action(self, *, actor: str, action: str, value: str, reason: str) -> dict:
        op_id = secrets.token_hex(12)
        self._record_operator_event_start(op_id=op_id, actor=actor, action=action, value=value, reason=reason)
        result_text = ""
        a = action.lower().strip()
        v = value.lower().strip()
        try:
            if a == "pause":
                await self._api_post("/pause")
                result_text = "paused"
            elif a == "resume":
                await self._api_post("/resume")
                result_text = "resumed_to_trading"
            elif a == "mode":
                if v not in {"dryrun", "paper", "live"}:
                    raise ValueError("mode must be one of: dryrun|paper|live")
                _write_ops_value("mode", v)
                result_text = f"mode={v}"
            elif a == "kill_switch":
                if v in {"on", "true", "1"}:
                    _write_ops_value("kill_switch", "true")
                    result_text = "kill_switch=on"
                elif v in {"off", "false", "0"}:
                    _write_ops_value("kill_switch", "false")
                    result_text = "kill_switch=off"
                else:
                    raise ValueError("kill-switch must be on|off")
            elif a == "chain_set":
                selection = parse_chain_selection(value)
                selection_name = f"{selection.family}:{selection.chain}"

                await self._set_state("PAUSED", actor=actor, reason=f"chain_switch_pause:{reason}")
                await self._api_post("/operator/chain", params={"name": selection_name})
                await self._set_state("SYNCING", actor=actor, reason=f"chain_switch_syncing:{reason}")
                result_text = f"chain_target_set selection={selection_name} (runtime hot-switch in progress)"
            else:
                raise ValueError(f"unknown action: {action}")

            effective_state, effective_chain = await self._read_effective_fields()
            self._record_operator_event_finish(
                op_id=op_id,
                applied=True,
                error=None,
                effective_state=effective_state,
                effective_chain=effective_chain,
            )
            await self._audit(
                f"operator_action op_id={op_id} actor={actor} action={a} value={v} reason={reason} result=success"
            )
            return {"op_id": op_id, "result": result_text}
        except Exception as e:
            effective_state, effective_chain = await self._read_effective_fields()
            self._record_operator_event_finish(
                op_id=op_id,
                applied=False,
                error=str(e),
                effective_state=effective_state,
                effective_chain=effective_chain,
            )
            await self._audit(
                f"operator_action op_id={op_id} actor={actor} action={a} value={v} reason={reason} result=fail err={e}"
            )
            raise


def _build_bot() -> OperatorBot:
    bot = OperatorBot()

    async def _chain_choices(prefix: str = "") -> list[str]:
        try:
            payload = await bot._api_get("/chains")
            items = payload.get("items", []) if isinstance(payload, dict) else []
            keys = [str(it.get("key", "")).strip() for it in items if str(it.get("key", "")).strip()]
            if prefix:
                p = prefix.strip().lower()
                keys = [k for k in keys if p in k.lower()]
            return keys[:25]
        except Exception:
            return []

    async def handle_status() -> str:
        text, _ = await bot._status_payload()
        return text

    async def handle_pause(actor: str, reason: str = "manual") -> str:
        out = await bot._apply_action(actor=actor, action="pause", value="true", reason=reason)
        return f"ok {out['result']} op_id={out['op_id']}"

    async def handle_resume(actor: str, reason: str = "manual") -> str:
        out = await bot._apply_action(actor=actor, action="resume", value="true", reason=reason)
        return f"ok {out['result']} op_id={out['op_id']}"

    async def handle_mode(actor: str, value: str, reason: str = "manual") -> str:
        out = await bot._apply_action(actor=actor, action="mode", value=value, reason=reason)
        return f"ok {out['result']} op_id={out['op_id']}"

    async def handle_kill(actor: str, value: str, reason: str = "manual") -> str:
        out = await bot._apply_action(actor=actor, action="kill_switch", value=value, reason=reason)
        return f"ok {out['result']} op_id={out['op_id']}"

    async def handle_chain(actor: str, value: str, reason: str = "manual") -> str:
        out = await bot._apply_action(actor=actor, action="chain_set", value=value, reason=reason)
        return f"ok {out['result']} op_id={out['op_id']}"

    async def handle_ops(limit: int = 20) -> str:
        resp = await bot._api_get("/operator/events", params={"limit": max(1, min(int(limit), 50))})
        rows = resp.get("items", []) if isinstance(resp, dict) else []
        if not rows:
            return "No operator actions found."
        lines = []
        for row in rows[:20]:
            op_id = str(row.get("op_id", "—"))
            action = str(row.get("action", "unknown"))
            applied = bool(row.get("applied", False))
            err = str(row.get("error", "") or "—")
            ts = str(row.get("ts", "") or row.get("created_at", "") or "—")
            lines.append(f"{ts} op_id={op_id} action={action} applied={applied} error={err}")
        return "\n".join(lines)

    async def _slash_exec(
        interaction: discord.Interaction,
        handler,
        *,
        ephemeral: bool = True,
    ) -> None:
        responded = False
        cmd_name = interaction.command.name if interaction.command else "unknown"
        try:
            await interaction.response.defer(ephemeral=ephemeral)
            responded = True
            if bot.command_channel_id and interaction.channel_id != bot.command_channel_id:
                await interaction.followup.send(
                    "Operator commands are restricted to the configured command channel.",
                    ephemeral=True,
                )
                log.info(
                    "slash_denied command=%s user_id=%s channel_id=%s instance_id=%s",
                    cmd_name,
                    interaction.user.id if interaction.user else 0,
                    interaction.channel_id,
                    bot.instance_id,
                )
                return
            result = await handler()
            await interaction.followup.send(f"{result}\ninstance_id={bot.instance_id}", ephemeral=ephemeral)
            log.info(
                "slash_ok command=%s user_id=%s channel_id=%s instance_id=%s responded=true",
                cmd_name,
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                bot.instance_id,
            )
        except Exception as e:
            log.exception(
                "slash_error command=%s user_id=%s channel_id=%s instance_id=%s",
                cmd_name,
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                bot.instance_id,
            )
            msg = f"Command failed: {e}"
            if responded:
                with contextlib.suppress(Exception):
                    await interaction.followup.send(msg, ephemeral=True)
            else:
                with contextlib.suppress(Exception):
                    await interaction.response.send_message(msg, ephemeral=True)

    @bot.command(name="status")
    async def status_cmd(ctx: commands.Context):
        if not await bot._check_channel(ctx):
            return
        await ctx.reply(await handle_status())

    @bot.command(name="pause")
    async def pause_cmd(ctx: commands.Context, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        await ctx.reply(await handle_pause(actor=str(ctx.author), reason=reason))

    @bot.command(name="resume")
    async def resume_cmd(ctx: commands.Context, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        await ctx.reply(await handle_resume(actor=str(ctx.author), reason=reason))

    @bot.command(name="mode")
    async def mode_cmd(ctx: commands.Context, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if bot._is_dangerous("mode", value):
            code = bot._new_confirm(user_id=ctx.author.id, action="mode", value=value, reason=reason)
            await ctx.reply(f"dangerous action pending: mode={value}. confirm with `!confirm {code}` within {bot.confirm_ttl_s}s")
            return
        await ctx.reply(await handle_mode(actor=str(ctx.author), value=value, reason=reason))

    @bot.command(name="kill-switch")
    async def kill_switch_cmd(ctx: commands.Context, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if bot._is_dangerous("kill_switch", value):
            code = bot._new_confirm(user_id=ctx.author.id, action="kill_switch", value=value, reason=reason)
            await ctx.reply(f"dangerous action pending: kill-switch {value}. confirm with `!confirm {code}` within {bot.confirm_ttl_s}s")
            return
        await ctx.reply(await handle_kill(actor=str(ctx.author), value=value, reason=reason))

    @bot.command(name="confirm")
    async def confirm_cmd(ctx: commands.Context, code: str):
        if not await bot._check_channel(ctx):
            return
        item = bot._take_confirm(user_id=ctx.author.id, code=code.strip().upper())
        if not item:
            await ctx.reply("invalid_or_expired_confirmation")
            return
        out = await bot._apply_action(
            actor=str(ctx.author),
            action=item.action,
            value=item.value,
            reason=f"confirmed:{item.reason}",
        )
        await ctx.reply(f"confirmed ok {out['result']} op_id={out['op_id']}")

    @bot.command(name="chain")
    async def chain_cmd(ctx: commands.Context, subcmd: str, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if subcmd.strip().lower() != "set":
            await ctx.reply("usage: !chain set EVM:sepolia|SOL:solana <reason>")
            return
        try:
            await ctx.reply(await handle_chain(actor=str(ctx.author), value=value, reason=reason))
        except Exception as e:
            await ctx.reply(f"error chain switch failed: {e}")

    @bot.command(name="ops")
    async def ops_cmd(ctx: commands.Context):
        if not await bot._check_channel(ctx):
            return
        await ctx.reply(await handle_ops(limit=20))

    @bot.tree.command(name="status", description="Show operator status")
    async def slash_status(interaction: discord.Interaction):
        await _slash_exec(interaction, handle_status, ephemeral=True)

    @bot.tree.command(name="pause", description="Pause trading")
    async def slash_pause(interaction: discord.Interaction):
        await _slash_exec(interaction, lambda: handle_pause(actor=str(interaction.user), reason="slash"), ephemeral=True)

    @bot.tree.command(name="resume", description="Resume trading")
    async def slash_resume(interaction: discord.Interaction):
        await _slash_exec(interaction, lambda: handle_resume(actor=str(interaction.user), reason="slash"), ephemeral=True)

    @bot.tree.command(name="mode", description="Set mode")
    @app_commands.describe(value="paper, dryrun, or live")
    async def slash_mode(interaction: discord.Interaction, value: str):
        async def _handler() -> str:
            if bot._is_dangerous("mode", value):
                return f"dangerous action pending in prefix flow only. use !mode {value} and !confirm"
            return await handle_mode(actor=str(interaction.user), value=value, reason="slash")
        await _slash_exec(interaction, _handler, ephemeral=True)

    @bot.tree.command(name="chain", description="Set chain selection, e.g. EVM:sepolia")
    @app_commands.describe(value="Chain selection like EVM:sepolia")
    async def slash_chain(interaction: discord.Interaction, value: str):
        await _slash_exec(interaction, lambda: handle_chain(actor=str(interaction.user), value=value, reason="slash"), ephemeral=True)

    @slash_chain.autocomplete("value")
    async def slash_chain_autocomplete(interaction: discord.Interaction, current: str):
        choices = await _chain_choices(current)
        return [app_commands.Choice(name=v, value=v) for v in choices]

    @bot.tree.command(name="ops", description="Show last 20 operator actions")
    async def slash_ops(interaction: discord.Interaction):
        await _slash_exec(interaction, lambda: handle_ops(limit=20), ephemeral=True)

    @bot.tree.command(name="ping", description="Operator bot health ping")
    async def slash_ping(interaction: discord.Interaction):
        async def _handler() -> str:
            return f"pong instance_id={bot.instance_id} api_base={bot.api_base} sync={bot.sync_mode}"
        await _slash_exec(interaction, _handler, ephemeral=True)

    return bot


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    token = os.getenv("DISCORD_OPERATOR_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_OPERATOR_TOKEN is required for discord operator bot")
    bot = _build_bot()
    log.info(
        "operator starting operator_impl=%s instance_id=%s api_base=%s",
        bot.operator_impl,
        bot.instance_id,
        bot.api_base,
    )
    bot.run(token)


if __name__ == "__main__":
    main()

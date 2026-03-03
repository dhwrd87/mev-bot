from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
import psycopg
from discord.ext import commands
import discord
from psycopg import errors as pg_errors

from bot.core.chain_adapter import parse_chain_selection, validate_chain_selection
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
        self.api_base = os.getenv("DISCORD_OPERATOR_API_BASE", "http://mev-bot:8000").rstrip("/")
        self.audit_channel_id = int(os.getenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "0") or "0")
        self.command_channel_id = int(os.getenv("DISCORD_OPERATOR_COMMAND_CHANNEL_ID", "0") or "0")
        self.confirm_ttl_s = int(os.getenv("DISCORD_OPERATOR_CONFIRM_TTL_S", "120"))
        self.status_channel_id = int(os.getenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", "0") or "0")
        self.status_refresh_s = int(os.getenv("DISCORD_OPERATOR_STATUS_REFRESH_S", "45"))
        self.pending: Dict[str, PendingConfirmation] = {}
        self.http = httpx.AsyncClient(timeout=8.0)
        self.status_card = StatusCardManager(
            bot=self,
            status_channel_id=self.status_channel_id,
            refresh_s=self.status_refresh_s,
            snapshot_fetcher=self._status_card_snapshot,
            kv_read=_read_ops_value,
            kv_write=_write_ops_value,
            audit_fn=self._audit,
        )

    async def close(self) -> None:
        await self.status_card.stop()
        await self.http.aclose()
        await super().close()

    async def on_ready(self):
        log.info(
            "discord-operator ready user=%s command_channel_id=%s status_channel_id=%s refresh_s=%s",
            self.user,
            self.command_channel_id,
            self.status_channel_id,
            self.status_refresh_s,
        )
        self.status_card.start()

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

    async def _api_get(self, path: str) -> dict:
        r = await self.http.get(f"{self.api_base}{path}")
        r.raise_for_status()
        return r.json()

    async def _api_post(self, path: str, params: dict | None = None) -> dict:
        r = await self.http.post(f"{self.api_base}{path}", params=params)
        r.raise_for_status()
        return r.json()

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
        metrics_resp = await self.http.get(f"{self.api_base}/metrics")
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
            "state": str(health.get("state", "UNKNOWN")),
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
        }
        msg = (
            f"state={payload['state']} chain=evm:{payload['chain']} paused={payload['paused']} "
            f"mode={payload['mode']} kill_switch={payload['kill_switch']} "
            f"rpc=({payload['rpc_health']}) "
            f"last_trade={payload['last_trade_time']} today_pnl={payload['today_pnl']} "
            f"error_rate={payload['error_rate_pct']:.2f}% "
            f"xlen={payload['xlen']:.0f} lag={payload['lag']:.0f} rpc429={payload['rpc_429_total']:.0f}"
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
            state=str(payload.get("state", "UNKNOWN")),
            mode=str(payload.get("mode", "paper")),
            chain_family=str(payload.get("chain_family", "evm")),
            chain=str(payload.get("chain", "unknown")),
            rpc_health=str(payload.get("rpc_health", "n/a")),
            last_trade_time=str(payload.get("last_trade_time", "n/a")),
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

    async def _apply_action(self, *, actor: str, action: str, value: str, reason: str) -> str:
        a = action.lower().strip()
        v = value.lower().strip()
        if a == "pause":
            await self._api_post("/pause")
            await self._audit(f"operator_action actor={actor} action=pause reason={reason}")
            return "paused"
        if a == "resume":
            await self._api_post("/resume")
            await self._audit(f"operator_action actor={actor} action=resume reason={reason}")
            return "resumed_to_trading"
        if a == "mode":
            if v not in {"dryrun", "paper", "live"}:
                raise ValueError("mode must be one of: dryrun|paper|live")
            _write_ops_value("mode", v)
            await self._audit(f"operator_action actor={actor} action=mode value={v} reason={reason}")
            return f"mode={v}"
        if a == "kill_switch":
            if v in {"on", "true", "1"}:
                _write_ops_value("kill_switch", "true")
                await self._audit(f"operator_action actor={actor} action=kill_switch value=on reason={reason}")
                return "kill_switch=on"
            if v in {"off", "false", "0"}:
                _write_ops_value("kill_switch", "false")
                await self._audit(f"operator_action actor={actor} action=kill_switch value=off reason={reason}")
                return "kill_switch=off"
            raise ValueError("kill-switch must be on|off")
        if a == "chain_set":
            selection = parse_chain_selection(value)
            selection_name = f"{selection.family}:{selection.chain}"

            await self._set_state("PAUSED", actor=actor, reason=f"chain_switch_pause:{reason}")
            _write_ops_value("chain_selection", selection_name)
            await self._api_post("/chain/select", params={"name": selection_name})
            await self._set_state("SYNCING", actor=actor, reason=f"chain_switch_syncing:{reason}")

            started = time.time()
            try:
                validation = await asyncio.to_thread(validate_chain_selection, selection)
            except Exception as e:
                await self._set_state("DEGRADED", actor="system", reason=f"chain_switch_validation_failed:{selection_name}")
                await self._audit(
                    f"operator_action actor={actor} action=chain_set value={selection_name} "
                    f"result=validation_failed reason={reason} err={e}"
                )
                raise

            elapsed_s = time.time() - started
            await self._set_state("READY", actor="system", reason=f"chain_switch_ready:{selection_name}")
            await self._audit(
                f"operator_action actor={actor} action=chain_set value={selection_name} result=ready "
                f"reason={reason} endpoint={validation.get('endpoint','')} wallet={validation.get('wallet','')} "
                f"balance={validation.get('balance','')} took_s={elapsed_s:.2f}"
            )
            return f"chain_switch_ready selection={selection_name} endpoint={validation.get('endpoint','')} took_s={elapsed_s:.2f} (manual resume required)"
        raise ValueError(f"unknown action: {action}")


def _build_bot() -> OperatorBot:
    bot = OperatorBot()

    @bot.command(name="status")
    async def status_cmd(ctx: commands.Context):
        if not await bot._check_channel(ctx):
            return
        text, _ = await bot._status_payload()
        await ctx.reply(text)

    @bot.command(name="pause")
    async def pause_cmd(ctx: commands.Context, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        out = await bot._apply_action(actor=str(ctx.author), action="pause", value="true", reason=reason)
        await ctx.reply(f"ok {out}")

    @bot.command(name="resume")
    async def resume_cmd(ctx: commands.Context, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        out = await bot._apply_action(actor=str(ctx.author), action="resume", value="true", reason=reason)
        await ctx.reply(f"ok {out}")

    @bot.command(name="mode")
    async def mode_cmd(ctx: commands.Context, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if bot._is_dangerous("mode", value):
            code = bot._new_confirm(user_id=ctx.author.id, action="mode", value=value, reason=reason)
            await ctx.reply(f"dangerous action pending: mode={value}. confirm with `!confirm {code}` within {bot.confirm_ttl_s}s")
            return
        out = await bot._apply_action(actor=str(ctx.author), action="mode", value=value, reason=reason)
        await ctx.reply(f"ok {out}")

    @bot.command(name="kill-switch")
    async def kill_switch_cmd(ctx: commands.Context, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if bot._is_dangerous("kill_switch", value):
            code = bot._new_confirm(user_id=ctx.author.id, action="kill_switch", value=value, reason=reason)
            await ctx.reply(f"dangerous action pending: kill-switch {value}. confirm with `!confirm {code}` within {bot.confirm_ttl_s}s")
            return
        out = await bot._apply_action(actor=str(ctx.author), action="kill_switch", value=value, reason=reason)
        await ctx.reply(f"ok {out}")

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
        await ctx.reply(f"confirmed ok {out}")

    @bot.command(name="chain")
    async def chain_cmd(ctx: commands.Context, subcmd: str, value: str, *, reason: str = "manual"):
        if not await bot._check_channel(ctx):
            return
        if subcmd.strip().lower() != "set":
            await ctx.reply("usage: !chain set EVM:sepolia|SOL:solana <reason>")
            return
        try:
            out = await bot._apply_action(actor=str(ctx.author), action="chain_set", value=value, reason=reason)
            await ctx.reply(f"ok {out}")
        except Exception as e:
            await ctx.reply(f"error chain switch failed: {e}")

    return bot


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    token = os.getenv("DISCORD_OPERATOR_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_OPERATOR_TOKEN is required for discord operator bot")
    bot = _build_bot()
    bot.run(token)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import discord
import httpx
from discord.ext import commands

from bot.core.canonical_chain import canonicalize_chain_target
from ops.confirmations import ConfirmationStore
from ops.discord_embeds import build_audit_embed, build_status_embed
from ops.operator_state import default_state, load_state, update_state
from ops.security import CommandGuard, UserRateLimiter, parse_id_csv
from ops.status_data import StatusDataProvider

log = logging.getLogger("ops.discord_operator")

PREFIX = "!"
STATUS_TITLE = "MEV Bot Status"


def _env_required(name: str) -> str:
    v = str(os.getenv(name, "")).strip()
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _as_int(name: str) -> int:
    raw = _env_required(name)
    try:
        return int(raw)
    except ValueError as e:
        raise SystemExit(f"Invalid integer env var {name}: {raw}") from e


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class OperatorBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)

        self.command_channel_id = _as_int("DISCORD_OPERATOR_COMMAND_CHANNEL_ID")
        self.audit_channel_id = _as_int("DISCORD_OPERATOR_AUDIT_CHANNEL_ID")
        self.status_channel_id = _as_int("DISCORD_OPERATOR_STATUS_CHANNEL_ID")
        self.status_refresh_s = max(15, int(os.getenv("DISCORD_OPERATOR_STATUS_REFRESH_S", "45")))
        self.state_path = os.getenv("OPERATOR_STATE_FILE", "ops/operator_state.json")
        self.metrics_url = str(
            os.getenv("DISCORD_OPERATOR_METRICS_SCRAPE_URL", os.getenv("METRICS_SCRAPE_URL", ""))
        ).strip()
        self.snapshot_path = str(os.getenv("DISCORD_OPERATOR_SNAPSHOT_PATH", "ops/health_snapshot.json")).strip()
        try:
            allowed_users = parse_id_csv(os.getenv("DISCORD_OPERATOR_ALLOWED_USER_IDS"))
            allowed_roles = parse_id_csv(os.getenv("DISCORD_OPERATOR_ALLOWED_ROLE_IDS"))
        except ValueError as e:
            raise SystemExit(f"Invalid operator allowlist env var: {e}") from e
        self.guard = CommandGuard(allowed_user_ids=allowed_users, allowed_role_ids=allowed_roles)
        self.rate_limiter = UserRateLimiter(limit=3, window_s=10.0)
        self.confirmations = ConfirmationStore(ttl_s=60.0)
        self._status_msg_id: Optional[int] = None
        self._status_msg: Optional[discord.Message] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._status_backoff_s: float = float(self.status_refresh_s)
        self._status_error_logged: bool = False
        self._snapshot_stale: Optional[bool] = None
        self._process_started = time.time()
        self.status_data = StatusDataProvider(
            metrics_scrape_url="",
            snapshot_path=self.snapshot_path,
            snapshot_stale_after_s=float(os.getenv("DISCORD_OPERATOR_SNAPSHOT_STALE_S", "60")),
        )
        self.httpx = httpx.AsyncClient(timeout=6.0)
        self._is_stopping = False

    async def close(self) -> None:
        if self._is_stopping:
            return
        self._is_stopping = True
        log.info("operator stopping")
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        await self.httpx.aclose()
        await super().close()

    async def on_ready(self) -> None:
        log.info(
            "operator ready command_channel_id=%s audit_channel_id=%s status_channel_id=%s",
            self.command_channel_id,
            self.audit_channel_id,
            self.status_channel_id,
        )
        if not self.guard.allowed_user_ids and not self.guard.allowed_role_ids:
            log.warning("operator command authorization is OPEN (no allowed users/roles configured)")
        if self._refresh_task is None or self._refresh_task.done():
            await self._bootstrap_status_card()
            self._ensure_refresh_task()

    def _ensure_refresh_task(self) -> bool:
        if self._refresh_task is not None and not self._refresh_task.done():
            return False
        self._refresh_task = asyncio.create_task(self._status_loop())
        return True

    async def _status_loop(self) -> None:
        while True:
            try:
                await self._refresh_status_card()
                if self._status_error_logged:
                    log.info("status card refresh recovered")
                self._status_error_logged = False
                self._status_backoff_s = float(self.status_refresh_s)
            except (discord.Forbidden, discord.HTTPException) as e:
                if not self._status_error_logged:
                    log.warning("status card refresh failed; backing off: %s", e)
                    self._status_error_logged = True
                self._status_backoff_s = min(max(float(self.status_refresh_s), self._status_backoff_s * 2.0), 900.0)
            except Exception as e:
                if not self._status_error_logged:
                    log.warning("status card refresh failed; backing off: %s", e)
                    self._status_error_logged = True
                self._status_backoff_s = min(max(float(self.status_refresh_s), self._status_backoff_s * 2.0), 900.0)
            await asyncio.sleep(self._status_backoff_s)

    async def _check_cmd_channel(self, ctx: commands.Context) -> bool:
        actor = self._actor(ctx)
        cmd = (ctx.message.content if ctx.message else "").strip() or str(getattr(ctx.command, "name", "unknown"))
        if not (ctx.channel and ctx.channel.id == self.command_channel_id):
            await ctx.reply("Commands are allowed only in the configured operator command channel.")
            await self._audit(actor=actor, command=cmd, result="denied:wrong_channel", ok=False)
            return False

        role_ids = []
        roles = getattr(ctx.author, "roles", None)
        if roles:
            role_ids = [int(r.id) for r in roles if getattr(r, "id", None)]
        allowed, reason = self.guard.authorize(user_id=int(ctx.author.id), role_ids=role_ids)
        if not allowed:
            await ctx.reply("Access denied.")
            await self._audit(actor=actor, command=cmd, result=f"denied:{reason}", ok=False)
            return False

        rate_ok, retry_after = self.rate_limiter.allow(int(ctx.author.id))
        if not rate_ok:
            await ctx.reply(f"Rate limited. Retry in {retry_after:.1f}s.")
            await self._audit(actor=actor, command=cmd, result=f"denied:rate_limited:{retry_after:.1f}s", ok=False)
            return False
        return True

    async def _audit(self, *, actor: str, command: str, result: str, ok: bool) -> None:
        payload = {
            "ts_utc": _utc_iso(),
            "actor": actor,
            "command": command,
            "result": result,
            "ok": ok,
        }
        ch = await self._resolve_text_channel(self.audit_channel_id)
        if ch:
            try:
                await ch.send(embed=build_audit_embed(payload))
            except Exception as e:
                log.warning("audit send failed: %s", e)

    async def _resolve_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        ch = self.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await self.fetch_channel(channel_id)
            return fetched if isinstance(fetched, discord.TextChannel) else None
        except Exception as e:
            log.warning("channel resolve failed id=%s err=%s", channel_id, e)
            return None

    def _actor(self, ctx: commands.Context) -> str:
        name = getattr(ctx.author, "display_name", str(ctx.author))
        return f"{ctx.author.id}:{name}"

    async def _build_snapshot(self) -> Dict[str, str]:
        st = load_state(self.state_path)
        bot_state = "PANIC" if st.get("kill_switch") else st.get("state", "UNKNOWN")
        data = await self.status_data.collect(self.httpx)
        active_chain = str(data.get("active_chain", "")).strip()
        if not active_chain or active_chain == "—":
            active_chain = str(st.get("chain_target", "UNKNOWN"))

        snapshot_status = str(data.get("snapshot_status", "OK"))
        out = {
            "bot_state": bot_state or "UNKNOWN",
            "mode": st.get("mode", "UNKNOWN"),
            "active_chain": active_chain,
            "heartbeat_utc": str(data.get("heartbeat_utc", "—")),
            "uptime": _fmt_uptime(time.time() - self._process_started),
            "head_slot_lag": str(data.get("head_slot_lag", "—")),
            "error_counters": str(data.get("error_counters", "—")),
            "latest_trade": str(data.get("last_trade", "—")),
            "rpc_p95": str(data.get("rpc_p95", "—")),
            "rpc_p99": str(data.get("rpc_p99", "—")),
            "trades_10m": str(data.get("trades_10m", "—")),
            "opportunities_10m": str(data.get("opportunities_10m", "—")),
            "confirm_p95": str(data.get("confirm_p95", "—")),
            "pnl_drawdown_fees": str(data.get("pnl_drawdown_fees", "—")),
            "snapshot_status": snapshot_status,
            "dex_health": str(data.get("dex_health", "—")),
            "__snapshot_stale": "1" if bool(data.get("stale", False)) else "0",
            "__snapshot_age_s": str(data.get("snapshot_age_s", "")),
        }
        if bool(data.get("stale", False)):
            out["error_counters"] = f"{out['error_counters']} [STALE]"
        return out

    async def _maybe_audit_snapshot_staleness(self, snapshot: Dict[str, str]) -> None:
        stale = str(snapshot.get("__snapshot_stale", "0")) == "1"
        age_raw = str(snapshot.get("__snapshot_age_s", "")).strip()
        age = "unknown"
        try:
            if age_raw:
                age = str(int(float(age_raw)))
        except Exception:
            age = "unknown"
        prev = self._snapshot_stale
        self._snapshot_stale = stale
        if prev is None or prev == stale:
            return
        if stale:
            await self._audit(
                actor="system",
                command="status_snapshot",
                result=f"stale age_s={age}",
                ok=False,
            )
            log.warning("status snapshot stale age_s=%s", age)
            return
        await self._audit(
            actor="system",
            command="status_snapshot",
            result="resumed",
            ok=True,
        )
        log.info("status snapshot freshness resumed")

    async def _refresh_status_card(self) -> None:
        if self._status_msg is None:
            await self._bootstrap_status_card()
        if self._status_msg is None:
            return
        snapshot = await self._build_snapshot()
        await self._maybe_audit_snapshot_staleness(snapshot)
        embed = build_status_embed(snapshot)
        try:
            await self._status_msg.edit(embed=embed)
        except discord.NotFound:
            self._status_msg = None
            self._status_msg_id = None
            _apply_state_change(self, {"status_message_id": None}, actor="system")
            await self._bootstrap_status_card()

    async def _bootstrap_status_card(self) -> None:
        channel = await self._resolve_text_channel(self.status_channel_id)
        if channel is None:
            return

        # 1) try persisted message id
        st = load_state(self.state_path)
        persisted_id = st.get("status_message_id")
        if persisted_id:
            try:
                msg = await channel.fetch_message(int(persisted_id))
                self._status_msg_id = msg.id
                self._status_msg = msg
                return
            except Exception:
                self._status_msg_id = None
                self._status_msg = None
                _apply_state_change(self, {"status_message_id": None}, actor="system")

        # 2) scan pins once (deprecated call replaced with async iterator)
        try:
            async for msg in channel.pins():
                if msg.author and msg.author.id == self.user.id:
                    if msg.embeds and msg.embeds[0].title == STATUS_TITLE:
                        self._status_msg_id = msg.id
                        self._status_msg = msg
                        _apply_state_change(self, {"status_message_id": msg.id}, actor="system")
                        return
        except Exception as e:
            log.warning("status pin scan failed: %s", e)

        # 3) create new status message; pin if allowed
        msg = await channel.send(embed=build_status_embed(await self._build_snapshot()))
        try:
            await msg.pin(reason="Operator status card")
        except discord.Forbidden:
            log.warning("missing pin permission in status channel; continuing with non-pinned status message")
        except discord.HTTPException as e:
            log.warning("status pin failed; continuing: %s", e)
        self._status_msg_id = msg.id
        self._status_msg = msg
        _apply_state_change(self, {"status_message_id": msg.id}, actor="system")

def _apply_state_change(bot: OperatorBot, patch: Dict[str, Any], actor: str) -> Dict[str, Any]:
    current = load_state(bot.state_path)
    merged = dict(current)
    merged.update(patch)
    return update_state(bot.state_path, merged, actor=actor)


def _is_dangerous_action(action: str, value: str) -> bool:
    a = str(action).strip().lower()
    v = str(value).strip().lower()
    if (a == "mode" and v == "live") or (a == "kill" and v == "off"):
        return True
    if a == "flashloan" and v == "enable":
        return True
    if a == "strategy" and v == "enable":
        return True
    if a == "limits" and v == "set":
        return True
    return False


def _slug_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [x.strip() for x in values.split(",")]
    if isinstance(values, (list, tuple, set)):
        out = []
        for v in values:
            s = str(v).strip().lower()
            if s:
                out.append(s)
        return sorted(set(out))
    return []


def _current_chain_slug(st: Dict[str, Any]) -> str:
    target = str(st.get("chain_target", "")).strip()
    if ":" in target:
        return target.split(":", 1)[1].strip().lower()
    env_chain = str(os.getenv("CHAIN", "")).strip().lower()
    return env_chain or "unknown"


def _load_chain_dex_cfg(chain: str) -> tuple[list[str], list[str]]:
    cfg_dir = Path(os.getenv("DEX_PACK_CONFIG_DIR", "config/chains"))
    p = cfg_dir / f"{chain}.yaml"
    if not p.exists():
        return [], []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    if not isinstance(raw, dict):
        return [], []
    enabled = _slug_list(raw.get("enabled_dex_packs"))
    dex_map = raw.get("dex_packs", {})
    available = sorted(set(str(k).strip().lower() for k in dex_map.keys())) if isinstance(dex_map, dict) else []
    return enabled, available


def _effective_dex_enabled(st: Dict[str, Any], defaults: list[str]) -> list[str]:
    overrides = st.get("enabled_dex_overrides") if isinstance(st.get("enabled_dex_overrides"), dict) else {}
    allow = _slug_list(overrides.get("allowlist"))
    deny = _slug_list(overrides.get("denylist"))
    if not allow:
        allow = _slug_list(st.get("dex_packs_enabled"))
    deny = sorted(set(deny + _slug_list(st.get("dex_packs_disabled"))))
    base = allow if allow else list(defaults)
    return sorted(x for x in set(base) if x not in set(deny))


def _risk_overrides(st: Dict[str, Any]) -> Dict[str, list[str]]:
    raw = st.get("risk_overrides") if isinstance(st.get("risk_overrides"), dict) else {}
    return {
        "allow_tokens": _slug_list(raw.get("allow_tokens")),
        "deny_tokens": _slug_list(raw.get("deny_tokens")),
        "watch_tokens": _slug_list(raw.get("watch_tokens")),
        "allow_pools": _slug_list(raw.get("allow_pools")),
        "deny_pools": _slug_list(raw.get("deny_pools")),
        "watch_pools": _slug_list(raw.get("watch_pools")),
    }


def _strategy_overrides(st: Dict[str, Any]) -> Dict[str, list[str]]:
    raw = st.get("strategy_overrides") if isinstance(st.get("strategy_overrides"), dict) else {}
    allow = _slug_list(raw.get("allowlist"))
    deny = _slug_list(raw.get("denylist"))
    if not allow:
        allow = _slug_list(st.get("strategy_enabled"))
    deny = sorted(set(deny + _slug_list(st.get("strategy_disabled"))))
    return {"allowlist": allow, "denylist": deny}


def _strategy_available() -> list[str]:
    raw = str(os.getenv("OPERATOR_STRATEGY_CATALOG", "")).strip()
    if raw:
        vals = _slug_list(raw)
        if vals:
            return vals
    return ["opportunity_engine", "hunter", "stealth", "flashloan_arb"]


def _to_opt_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return float(s)


def _limits(st: Dict[str, Any]) -> Dict[str, float | None]:
    raw = st.get("limits") if isinstance(st.get("limits"), dict) else {}
    return {
        "max_fee_gwei": _to_opt_float(raw.get("max_fee_gwei")),
        "slippage_bps": _to_opt_float(raw.get("slippage_bps")),
        "max_daily_loss_usd": _to_opt_float(raw.get("max_daily_loss_usd")),
        "min_edge_bps": _to_opt_float(raw.get("min_edge_bps")),
    }


def _norm_limit_key(k: str) -> str:
    key = str(k or "").strip().lower()
    aliases = {
        "max_fee": "max_fee_gwei",
        "max_fee_gwei": "max_fee_gwei",
        "slippage": "slippage_bps",
        "slippage_bps": "slippage_bps",
        "daily_loss": "max_daily_loss_usd",
        "max_daily_loss": "max_daily_loss_usd",
        "max_daily_loss_usd": "max_daily_loss_usd",
        "min_edge": "min_edge_bps",
        "min_edge_bps": "min_edge_bps",
    }
    return aliases.get(key, "")


def _confirm_embed(*, action: str, args: Dict[str, Any], token: str, expires_s: int = 60) -> discord.Embed:
    em = discord.Embed(
        title="Confirmation Required",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Action", value=action, inline=False)
    em.add_field(name="Args", value=str(args), inline=False)
    em.add_field(name="Token", value=token, inline=False)
    em.add_field(name="Confirm", value=f"`!confirm {token}` (expires in {expires_s}s)", inline=False)
    return em


def _reason_counts_from_metrics(metrics_text: str, metric_name: str, *, top_n: int = 5) -> list[tuple[str, float]]:
    counts: Dict[str, float] = {}
    for raw in metrics_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(metric_name):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        value = 0.0
        try:
            value = float(parts[-1])
        except Exception:
            continue
        reason = "unknown"
        mark = 'reason="'
        if mark in line:
            p = line.split(mark, 1)[1]
            end = p.find('"')
            if end >= 0:
                reason = p[:end]
        counts[reason] = counts.get(reason, 0.0) + value
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[: max(1, int(top_n))]


def build_bot() -> OperatorBot:
    bot = OperatorBot()

    @bot.command(name="help")
    async def help_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        text = (
            "`!help`, `!status`, `!pause`, `!resume`, `!kill on|off`, "
            "`!mode dryrun|paper|live`, `!chain set <name>`, "
            "`!strategy list|enable <name>|disable <name>`, "
            "`!opps top`, `!limits show|set <key> <value>`, `!flashloan enable|disable`, "
            "`!report last10`, "
            "`!dex list|status|enable <dex>|disable <dex>`, "
            "`!risk allow|deny|watch token|pool <id>`, `!risk report <id>`, "
            "`!confirm <token>`, `!config`, `!ping`"
        )
        await ctx.reply(text)

    @bot.command(name="status")
    async def status_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        await ctx.reply(embed=build_status_embed(await bot._build_snapshot()))

    @bot.command(name="pause")
    async def pause_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        try:
            _apply_state_change(bot, {"state": "PAUSED"}, actor=actor)
            await ctx.reply("ok state=PAUSED")
            await bot._audit(actor=actor, command="!pause", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command="!pause", result=f"fail:{e}", ok=False)

    @bot.command(name="resume")
    async def resume_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        try:
            _apply_state_change(bot, {"state": "TRADING"}, actor=actor)
            await ctx.reply("ok state=TRADING")
            await bot._audit(actor=actor, command="!resume", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command="!resume", result=f"fail:{e}", ok=False)

    @bot.command(name="kill")
    async def kill_cmd(ctx: commands.Context, value: str):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        actor_id = int(ctx.author.id)
        v = value.strip().lower()
        if v not in {"on", "off"}:
            await ctx.reply("usage: !kill on|off")
            return
        if _is_dangerous_action("kill", v):
            c = bot.confirmations.create(action="kill", args={"value": v}, actor_id=actor_id)
            await ctx.reply(embed=_confirm_embed(action="kill", args={"value": v}, token=c.token))
            await bot._audit(actor=actor, command=f"!kill {v}", result=f"pending_confirmation:{c.token}", ok=False)
            return
        try:
            if v == "on":
                _apply_state_change(bot, {"kill_switch": True, "state": "PANIC"}, actor=actor)
            else:
                st = load_state(bot.state_path)
                next_state = "PAUSED" if st.get("state") == "PANIC" else st.get("state", "UNKNOWN")
                _apply_state_change(bot, {"kill_switch": False, "state": next_state}, actor=actor)
            await ctx.reply(f"ok kill_switch={v}")
            await bot._audit(actor=actor, command=f"!kill {v}", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!kill {v}", result=f"fail:{e}", ok=False)

    @bot.command(name="mode")
    async def mode_cmd(ctx: commands.Context, value: str):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        actor_id = int(ctx.author.id)
        mode = value.strip().lower()
        if mode not in {"dryrun", "paper", "live"}:
            await ctx.reply("usage: !mode dryrun|paper|live")
            return
        if _is_dangerous_action("mode", mode):
            c = bot.confirmations.create(action="mode", args={"value": mode}, actor_id=actor_id)
            await ctx.reply(embed=_confirm_embed(action="mode", args={"value": mode}, token=c.token))
            await bot._audit(actor=actor, command=f"!mode {mode}", result=f"pending_confirmation:{c.token}", ok=False)
            return
        try:
            _apply_state_change(bot, {"mode": mode}, actor=actor)
            await ctx.reply(f"ok mode={mode}")
            await bot._audit(actor=actor, command=f"!mode {mode}", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!mode {mode}", result=f"fail:{e}", ok=False)

    @bot.command(name="confirm")
    async def confirm_cmd(ctx: commands.Context, token: str):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        token_str = token.strip().upper()
        ok, item, reason = bot.confirmations.consume(token=token_str, actor_id=int(ctx.author.id))
        if not ok:
            await ctx.reply(f"confirmation rejected: {reason}")
            await bot._audit(actor=actor, command=f"!confirm {token_str}", result=f"denied:{reason}", ok=False)
            return
        try:
            if item.action == "mode":
                mode = str(item.args.get("value", "")).strip().lower()
                _apply_state_change(bot, {"mode": mode}, actor=actor)
                await ctx.reply(f"confirmed ok mode={mode}")
                await bot._audit(actor=actor, command=f"!confirm {token_str}", result=f"success:mode={mode}", ok=True)
                return
            if item.action == "kill":
                v = str(item.args.get("value", "")).strip().lower()
                if v == "on":
                    _apply_state_change(bot, {"kill_switch": True, "state": "PANIC"}, actor=actor)
                elif v == "off":
                    st = load_state(bot.state_path)
                    next_state = "PAUSED" if st.get("state") == "PANIC" else st.get("state", "UNKNOWN")
                    _apply_state_change(bot, {"kill_switch": False, "state": next_state}, actor=actor)
                await ctx.reply(f"confirmed ok kill_switch={v}")
                await bot._audit(actor=actor, command=f"!confirm {token_str}", result=f"success:kill={v}", ok=True)
                return
            if item.action == "strategy":
                act = str(item.args.get("value", "")).strip().lower()
                strategy = str(item.args.get("strategy", "")).strip().lower()
                st = load_state(bot.state_path)
                so = _strategy_overrides(st)
                allow = _slug_list(so.get("allowlist"))
                deny = _slug_list(so.get("denylist"))
                if act == "enable":
                    if allow:
                        allow = sorted(set(allow + [strategy]))
                    deny = sorted(x for x in deny if x != strategy)
                elif act == "disable":
                    deny = sorted(set(deny + [strategy]))
                    allow = sorted(x for x in allow if x != strategy)
                else:
                    raise ValueError(f"unsupported strategy action {act}")
                _apply_state_change(
                    bot,
                    {
                        "strategy_overrides": {"allowlist": allow, "denylist": deny},
                        "strategy_enabled": allow,
                        "strategy_disabled": deny,
                    },
                    actor=actor,
                )
                await ctx.reply(f"confirmed ok strategy_{act}={strategy}")
                await bot._audit(
                    actor=actor,
                    command=f"!confirm {token_str}",
                    result=f"success:strategy_{act}={strategy}",
                    ok=True,
                )
                return
            if item.action == "limits":
                key = _norm_limit_key(str(item.args.get("key", "")))
                if not key:
                    raise ValueError("invalid_limit_key")
                value = float(item.args.get("value"))
                st = load_state(bot.state_path)
                lim = _limits(st)
                lim[key] = value
                _apply_state_change(bot, {"limits": lim}, actor=actor)
                await ctx.reply(f"confirmed ok limits {key}={value}")
                await bot._audit(
                    actor=actor,
                    command=f"!confirm {token_str}",
                    result=f"success:limits_{key}={value}",
                    ok=True,
                )
                return
            if item.action == "flashloan":
                v = str(item.args.get("value", "")).strip().lower()
                enabled = v == "enable"
                _apply_state_change(bot, {"flashloan_enabled": enabled}, actor=actor)
                await ctx.reply(f"confirmed ok flashloan_enabled={enabled}")
                await bot._audit(
                    actor=actor,
                    command=f"!confirm {token_str}",
                    result=f"success:flashloan={v}",
                    ok=True,
                )
                return
            await ctx.reply(f"confirmation rejected: unsupported_action:{item.action}")
            await bot._audit(
                actor=actor,
                command=f"!confirm {token_str}",
                result=f"denied:unsupported_action:{item.action}",
                ok=False,
            )
        except Exception as e:
            await ctx.reply(f"confirmation failed: {e}")
            await bot._audit(actor=actor, command=f"!confirm {token_str}", result=f"fail:{e}", ok=False)

    @bot.command(name="chain")
    async def chain_cmd(ctx: commands.Context, action: str, *, name: str):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        if action.strip().lower() != "set":
            await ctx.reply("usage: !chain set <name>")
            return
        target_raw = name.strip()
        target = canonicalize_chain_target(target_raw)
        if target == "UNKNOWN":
            await ctx.reply("usage: !chain set <canonical-slug>; examples: sepolia, base, berachain, solana")
            await bot._audit(actor=actor, command=f"!chain set {target_raw}", result="denied:invalid_chain", ok=False)
            return
        try:
            st = load_state(bot.state_path)
            old = str(st.get("chain_target", "UNKNOWN"))
            _apply_state_change(bot, {"chain_target": target, "state": "PAUSED"}, actor=actor)
            await ctx.reply(f"ok chain_target={target} state=PAUSED (manual !resume required)")
            await bot._audit(
                actor=actor,
                command=f"!chain set {target}",
                result=f"success old_chain={old} new_chain={target} forced_state=PAUSED",
                ok=True,
            )
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!chain set {target}", result=f"fail:{e}", ok=False)

    @bot.command(name="config")
    async def config_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        st = load_state(bot.state_path)
        lim = _limits(st)
        strat = _strategy_overrides(st)
        text = (
            f"state={st.get('state','UNKNOWN')} mode={st.get('mode','UNKNOWN')} "
            f"kill_switch={st.get('kill_switch',False)} chain_target={st.get('chain_target','UNKNOWN')} "
            f"flashloan_enabled={bool(st.get('flashloan_enabled', False))} "
            f"strategies_allow={strat.get('allowlist') or ['—']} strategies_deny={strat.get('denylist') or ['—']} "
            f"limits={lim} state_file={bot.state_path} metrics_url={bot.metrics_url or '—'}"
        )
        await ctx.reply(text)

    @bot.command(name="strategy")
    async def strategy_cmd(ctx: commands.Context, action: str = "", value: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        actor_id = int(ctx.author.id)
        act = str(action or "").strip().lower()
        strategy = str(value or "").strip().lower()
        st = load_state(bot.state_path)
        so = _strategy_overrides(st)
        available = _strategy_available()

        if act in {"", "list"}:
            effective = sorted(set((so["allowlist"] or available)) - set(so["denylist"]))
            await ctx.reply(
                f"available={available or ['—']} enabled={effective or ['—']} "
                f"allowlist={so['allowlist'] or ['—']} denylist={so['denylist'] or ['—']}"
            )
            await bot._audit(actor=actor, command="!strategy list", result="success", ok=True)
            return

        if act not in {"enable", "disable"} or not strategy:
            await ctx.reply("usage: !strategy list | !strategy enable <name> | !strategy disable <name>")
            return

        if strategy not in available:
            await ctx.reply(f"unknown strategy '{strategy}'. available={available}")
            await bot._audit(actor=actor, command=f"!strategy {act} {strategy}", result="denied:unknown_strategy", ok=False)
            return

        if _is_dangerous_action("strategy", act):
            c = bot.confirmations.create(action="strategy", args={"value": act, "strategy": strategy}, actor_id=actor_id)
            await ctx.reply(embed=_confirm_embed(action="strategy", args={"value": act, "strategy": strategy}, token=c.token))
            await bot._audit(actor=actor, command=f"!strategy {act} {strategy}", result=f"pending_confirmation:{c.token}", ok=False)
            return

        try:
            allow = _slug_list(so.get("allowlist"))
            deny = _slug_list(so.get("denylist"))
            if act == "enable":
                if allow:
                    allow = sorted(set(allow + [strategy]))
                deny = sorted(x for x in deny if x != strategy)
            else:
                deny = sorted(set(deny + [strategy]))
                allow = sorted(x for x in allow if x != strategy)
            patch = {
                "strategy_overrides": {"allowlist": allow, "denylist": deny},
                "strategy_enabled": allow,
                "strategy_disabled": deny,
            }
            _apply_state_change(bot, patch, actor=actor)
            await ctx.reply(f"ok strategy_{act}={strategy} allow={allow or ['—']} deny={deny or ['—']}")
            await bot._audit(actor=actor, command=f"!strategy {act} {strategy}", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!strategy {act} {strategy}", result=f"fail:{e}", ok=False)

    @bot.command(name="opps")
    async def opps_cmd(ctx: commands.Context, action: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        act = str(action or "").strip().lower()
        if act != "top":
            await ctx.reply("usage: !opps top")
            return
        try:
            out = await bot.status_data.collect(bot.httpx)
            rows = []
            snap_path = Path(bot.snapshot_path)
            if snap_path.exists():
                with contextlib.suppress(Exception):
                    raw = json.loads(snap_path.read_text(encoding="utf-8"))
                    top = raw.get("top_opportunities") if isinstance(raw, dict) else None
                    if isinstance(top, list) and top:
                        for item in top[:5]:
                            if not isinstance(item, dict):
                                continue
                            typ = str(item.get("type", "unknown"))
                            est = item.get("est_profit_usd", item.get("profit_est_usd", "—"))
                            conf = item.get("confidence", "—")
                            rows.append(f"type={typ} est_profit_usd={est} confidence={conf}")
            if not rows:
                rows.append(
                    f"opportunities_10m={out.get('opportunities_10m','—')} top_opportunities=unavailable_in_snapshot"
                )
            await ctx.reply("\n".join(rows))
            await bot._audit(actor=actor, command="!opps top", result=f"success rows={len(rows)}", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command="!opps top", result=f"fail:{e}", ok=False)

    @bot.command(name="limits")
    async def limits_cmd(ctx: commands.Context, action: str = "", key: str = "", value: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        actor_id = int(ctx.author.id)
        act = str(action or "").strip().lower()
        st = load_state(bot.state_path)
        lim = _limits(st)
        if act in {"", "show"}:
            await ctx.reply(
                f"limits max_fee_gwei={lim['max_fee_gwei']} slippage_bps={lim['slippage_bps']} "
                f"max_daily_loss_usd={lim['max_daily_loss_usd']} min_edge_bps={lim['min_edge_bps']}"
            )
            await bot._audit(actor=actor, command="!limits show", result="success", ok=True)
            return
        if act != "set":
            await ctx.reply("usage: !limits show | !limits set <max_fee_gwei|slippage_bps|max_daily_loss_usd|min_edge_bps> <value>")
            return
        k = _norm_limit_key(key)
        if not k:
            await ctx.reply("invalid key; use max_fee_gwei|slippage_bps|max_daily_loss_usd|min_edge_bps")
            return
        try:
            v = float(str(value).strip())
        except Exception:
            await ctx.reply(f"invalid numeric value: {value}")
            return
        if _is_dangerous_action("limits", "set"):
            c = bot.confirmations.create(action="limits", args={"key": k, "value": v}, actor_id=actor_id)
            await ctx.reply(embed=_confirm_embed(action="limits", args={"key": k, "value": v}, token=c.token))
            await bot._audit(actor=actor, command=f"!limits set {k} {v}", result=f"pending_confirmation:{c.token}", ok=False)
            return
        try:
            lim[k] = v
            _apply_state_change(bot, {"limits": lim}, actor=actor)
            await ctx.reply(f"ok limits {k}={v}")
            await bot._audit(actor=actor, command=f"!limits set {k} {v}", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!limits set {k} {v}", result=f"fail:{e}", ok=False)

    @bot.command(name="flashloan")
    async def flashloan_cmd(ctx: commands.Context, action: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        actor_id = int(ctx.author.id)
        act = str(action or "").strip().lower()
        if act not in {"enable", "disable"}:
            await ctx.reply("usage: !flashloan enable|disable")
            return
        if _is_dangerous_action("flashloan", act):
            c = bot.confirmations.create(action="flashloan", args={"value": act}, actor_id=actor_id)
            await ctx.reply(embed=_confirm_embed(action="flashloan", args={"value": act}, token=c.token))
            await bot._audit(actor=actor, command=f"!flashloan {act}", result=f"pending_confirmation:{c.token}", ok=False)
            return
        try:
            enabled = act == "enable"
            _apply_state_change(bot, {"flashloan_enabled": enabled}, actor=actor)
            await ctx.reply(f"ok flashloan_enabled={enabled}")
            await bot._audit(actor=actor, command=f"!flashloan {act}", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!flashloan {act}", result=f"fail:{e}", ok=False)

    @bot.command(name="report")
    async def report_cmd(ctx: commands.Context, action: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        act = str(action or "").strip().lower()
        if act != "last10":
            await ctx.reply("usage: !report last10")
            return
        if not bot.metrics_url:
            await ctx.reply("metrics scrape url not configured (`DISCORD_OPERATOR_METRICS_SCRAPE_URL`)")
            await bot._audit(actor=actor, command="!report last10", result="denied:no_metrics_url", ok=False)
            return
        try:
            resp = await bot.httpx.get(bot.metrics_url)
            resp.raise_for_status()
            txt = resp.text
            sim = _reason_counts_from_metrics(txt, "mevbot_sim_fail_total")
            txf = _reason_counts_from_metrics(txt, "mevbot_tx_failed_total")
            sim_txt = ", ".join([f"{k}:{int(v)}" for k, v in sim]) if sim else "—"
            txf_txt = ", ".join([f"{k}:{int(v)}" for k, v in txf]) if txf else "—"
            await ctx.reply(f"sim_fail_reasons(last10~approx) {sim_txt}\ntx_fail_reasons(last10~approx) {txf_txt}")
            await bot._audit(actor=actor, command="!report last10", result="success", ok=True)
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command="!report last10", result=f"fail:{e}", ok=False)

    @bot.command(name="dex")
    async def dex_cmd(ctx: commands.Context, action: str = "", value: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        act = str(action or "").strip().lower()
        st = load_state(bot.state_path)
        chain = _current_chain_slug(st)
        defaults_enabled, available = _load_chain_dex_cfg(chain)

        if act in {"", "list"}:
            effective = _effective_dex_enabled(st, defaults_enabled)
            await ctx.reply(
                f"chain={chain} enabled={effective or ['—']} available={available or ['—']} "
                f"defaults={defaults_enabled or ['—']}"
            )
            await bot._audit(actor=actor, command="!dex list", result=f"success chain={chain}", ok=True)
            return

        if act == "status":
            data = await bot.status_data.collect(bot.httpx)
            rows = []
            raw = data.get("dex_health_summary")
            if isinstance(raw, dict) and raw:
                for dex in sorted(raw.keys()):
                    item = raw.get(dex) or {}
                    rows.append(
                        f"{dex}: quote_p95_ms={item.get('quote_p95_ms','—')} quote_fail_10m={item.get('quote_fail_10m','—')}"
                    )
            elif str(data.get("dex_health", "—")) != "—":
                rows.append(str(data.get("dex_health")))
            if not rows:
                rows = ["no_dex_snapshot_data (check health snapshot writer)"]
            await ctx.reply("\n".join(rows[:10]))
            await bot._audit(actor=actor, command="!dex status", result=f"success rows={len(rows)}", ok=True)
            return

        dex = str(value or "").strip().lower()
        if act not in {"enable", "disable"} or not dex:
            await ctx.reply("usage: !dex list | !dex status | !dex enable <dex> | !dex disable <dex>")
            return

        try:
            current = st.get("enabled_dex_overrides") if isinstance(st.get("enabled_dex_overrides"), dict) else {}
            allow = _slug_list(current.get("allowlist"))
            deny = _slug_list(current.get("denylist"))
            if act == "enable":
                if allow:
                    allow = sorted(set(allow + [dex]))
                deny = sorted(x for x in deny if x != dex)
            else:
                deny = sorted(set(deny + [dex]))
                allow = sorted(x for x in allow if x != dex)

            patch = {
                "enabled_dex_overrides": {"allowlist": allow, "denylist": deny},
                # backward-compatible mirrors consumed by some codepaths
                "dex_packs_enabled": allow,
                "dex_packs_disabled": deny,
            }
            _apply_state_change(bot, patch, actor=actor)
            effective = _effective_dex_enabled(load_state(bot.state_path), defaults_enabled)
            await ctx.reply(
                f"ok dex_{act}={dex} chain={chain} allowlist={allow or ['—']} denylist={deny or ['—']} "
                f"effective={effective or ['—']}"
            )
            await bot._audit(
                actor=actor,
                command=f"!dex {act} {dex}",
                result=f"success chain={chain} allow={allow} deny={deny}",
                ok=True,
            )
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!dex {act} {dex}", result=f"fail:{e}", ok=False)

    @bot.command(name="ping")
    async def ping_cmd(ctx: commands.Context):
        if not await bot._check_cmd_channel(ctx):
            return
        ws_ms = round(bot.latency * 1000.0, 1)
        await ctx.reply(f"pong ws_latency_ms={ws_ms}")

    @bot.command(name="risk")
    async def risk_cmd(ctx: commands.Context, action: str = "", scope: str = "", value: str = ""):
        if not await bot._check_cmd_channel(ctx):
            return
        actor = bot._actor(ctx)
        act = str(action or "").strip().lower()
        scp = str(scope or "").strip().lower()
        val = str(value or "").strip().lower()
        st = load_state(bot.state_path)
        ov = _risk_overrides(st)

        if act == "report":
            target = scp or val
            rows = [
                f"target={target or '—'}",
                f"allow_tokens={ov['allow_tokens'] or ['—']}",
                f"deny_tokens={ov['deny_tokens'] or ['—']}",
                f"watch_tokens={ov['watch_tokens'] or ['—']}",
                f"allow_pools={ov['allow_pools'] or ['—']}",
                f"deny_pools={ov['deny_pools'] or ['—']}",
                f"watch_pools={ov['watch_pools'] or ['—']}",
            ]
            await ctx.reply("\n".join(rows))
            await bot._audit(actor=actor, command=f"!risk report {target}".strip(), result="success", ok=True)
            return

        if act not in {"allow", "deny", "watch"} or scp not in {"token", "pool"} or not val:
            await ctx.reply("usage: !risk allow|deny|watch token|pool <id> | !risk report <id>")
            return

        key = f"{act}_{scp}s"  # allow_tokens / deny_pools / ...
        # Remove target from all buckets of same scope first, then add to selected.
        for k in [f"allow_{scp}s", f"deny_{scp}s", f"watch_{scp}s"]:
            ov[k] = sorted(x for x in ov.get(k, []) if x != val)
        ov[key] = sorted(set(ov.get(key, []) + [val]))
        try:
            _apply_state_change(bot, {"risk_overrides": ov}, actor=actor)
            await ctx.reply(f"ok risk_{act} {scp}={val}")
            await bot._audit(
                actor=actor,
                command=f"!risk {act} {scp} {val}",
                result=f"success {key}={ov[key]}",
                ok=True,
            )
        except Exception as e:
            await ctx.reply(f"fail: {e}")
            await bot._audit(actor=actor, command=f"!risk {act} {scp} {val}", result=f"fail:{e}", ok=False)

    return bot


def main() -> None:
    _validate_required_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    log.info("operator starting")
    token = _env_required("DISCORD_OPERATOR_TOKEN")
    _ = _as_int("DISCORD_OPERATOR_COMMAND_CHANNEL_ID")
    _ = _as_int("DISCORD_OPERATOR_AUDIT_CHANNEL_ID")
    _ = _as_int("DISCORD_OPERATOR_STATUS_CHANNEL_ID")

    state_path = os.getenv("OPERATOR_STATE_FILE", "ops/operator_state.json")
    if not os.path.exists(state_path):
        update_state(state_path, default_state(), actor="system")

    asyncio.run(_run_bot(token))


def _validate_required_env() -> None:
    required = [
        "DISCORD_OPERATOR_TOKEN",
        "DISCORD_OPERATOR_COMMAND_CHANNEL_ID",
        "DISCORD_OPERATOR_AUDIT_CHANNEL_ID",
        "DISCORD_OPERATOR_STATUS_CHANNEL_ID",
    ]
    missing = [k for k in required if not str(os.getenv(k, "")).strip()]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")


async def _run_bot(token: str) -> None:
    bot = build_bot()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str) -> None:
        log.info("operator stopping signal=%s", sig_name)
        if not stop_event.is_set():
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig.name)

    start_task = asyncio.create_task(bot.start(token))
    stop_task = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    if stop_task in done and not start_task.done():
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
    else:
        with contextlib.suppress(Exception):
            await start_task

    await bot.close()
    stop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stop_task


if __name__ == "__main__":
    main()

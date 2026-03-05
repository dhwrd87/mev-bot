from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
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
from ops import discord_embeds as ui


log = logging.getLogger("discord-operator")


def _panel_state_path() -> Path:
    raw = str(os.getenv("DISCORD_OPERATOR_PANEL_STATE_PATH", "runtime/operator_panel.json")).strip()
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _load_panel_state_file() -> dict[str, Any]:
    p = _panel_state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_panel_state_file(*, panel_message_id: int, panel_channel_id: int) -> None:
    p = _panel_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    payload = {
        "panel_message_id": int(panel_message_id),
        "panel_channel_id": int(panel_channel_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


def _ops_dsn() -> str:
    return str(os.getenv("DATABASE_URL", "")).strip()


def _ops_state_path() -> Path:
    raw = str(os.getenv("DISCORD_OPERATOR_STATE_PATH", "runtime/operator_state_runtime.json")).strip()
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _read_ops_state_file() -> dict[str, str]:
    p = _ops_state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _write_ops_state_file(data: dict[str, str]) -> None:
    p = _ops_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


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
    file_state = _read_ops_state_file()
    file_default = str(file_state.get(key, default))
    dsn = _ops_dsn()
    if not dsn:
        return file_default
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _ensure_ops_state_table(conn)
            row = conn.execute("SELECT v FROM ops_state WHERE k=%s", (key,)).fetchone()
            if not row:
                return file_default
            return str(row[0])
    except Exception:
        return file_default


def _write_ops_value(key: str, value: str) -> None:
    st = _read_ops_state_file()
    st[str(key)] = str(value)
    _write_ops_state_file(st)
    dsn = _ops_dsn()
    if not dsn:
        return
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _ensure_ops_state_table(conn)
            conn.execute(
                """
                INSERT INTO ops_state(k, v, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()
                """,
                (key, value),
            )
    except Exception:
        return


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


class OperatorPanelView(discord.ui.View):
    def __init__(self, bot: "OperatorBot") -> None:
        super().__init__(timeout=None)
        self.bot_ref = bot

    async def _precheck(self, interaction: discord.Interaction, action: str) -> bool:
        log.info(
            "interaction_received type=component custom_id=%s user_id=%s channel_id=%s instance_id=%s",
            action,
            interaction.user.id if interaction.user else 0,
            interaction.channel_id,
            self.bot_ref.instance_id,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        ok, reason = await self.bot_ref._authorize_interaction(interaction, f"panel:{action}")
        if not ok:
            with contextlib.suppress(Exception):
                await interaction.followup.send(
                    embed=ui.build_not_authorized_embed(
                        reason=reason,
                        policy=self.bot_ref._auth_policy_summary(),
                        instance_id=self.bot_ref.instance_id,
                    ),
                    ephemeral=True,
                )
            return False
        expected = self.bot_ref.panel_message_id
        msg = interaction.message
        if expected and (msg is None or int(msg.id) != int(expected)):
            with contextlib.suppress(Exception):
                await interaction.followup.send(
                    embed=ui.build_not_authorized_embed(
                        reason="stale_panel_message",
                        policy="Run /panel to recreate or refresh the active control panel.",
                        instance_id=self.bot_ref.instance_id,
                    ),
                    ephemeral=True,
                )
            return False
        return True

    async def _refresh_embed(self, interaction: discord.Interaction) -> None:
        msg = interaction.message
        if msg is None:
            return
        _, payload = await self.bot_ref._status_payload()
        embed = self.bot_ref._build_panel_embed(payload)
        await msg.edit(embed=embed, view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, custom_id="operator_panel_refresh")
    async def refresh_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "refresh"):
            return
        try:
            await self._refresh_embed(interaction)
            await interaction.followup.send("ok refreshed", ephemeral=True)
        except Exception:
            self.bot_ref.last_error = "panel_refresh_failed"
            log.exception("interaction_error action=panel_refresh instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send("Command failed: panel refresh failed", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.danger, custom_id="operator_panel_pause")
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "pause"):
            return
        try:
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="pause", value="true", reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok paused op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_pause instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, custom_id="operator_panel_resume")
    async def resume_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "resume"):
            return
        try:
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="resume", value="true", reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok resumed op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_resume instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Kill ON", style=discord.ButtonStyle.danger, custom_id="operator_panel_kill_on")
    async def kill_on_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "kill_on"):
            return
        try:
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="kill_switch", value="on", reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok kill_switch on op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_kill_on instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Kill OFF", style=discord.ButtonStyle.secondary, custom_id="operator_panel_kill_off")
    async def kill_off_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "kill_off"):
            return
        try:
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="kill_switch", value="off", reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok kill_switch off op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_kill_off instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.select(
        placeholder="Select mode",
        min_values=1,
        max_values=1,
        custom_id="operator_panel_mode_select",
        options=[
            discord.SelectOption(label="paper", value="paper"),
            discord.SelectOption(label="dryrun", value="dryrun"),
            discord.SelectOption(label="live", value="live"),
        ],
    )
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._precheck(interaction, "mode"):
            return
        value = str(select.values[0]).strip().lower() if select.values else "paper"
        try:
            if self.bot_ref._is_dangerous("mode", value):
                await interaction.followup.send("dangerous action pending in prefix flow only. use !mode live and !confirm", ephemeral=True)
                return
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="mode", value=value, reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok mode={value} op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_mode instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.select(
        placeholder="Select chain",
        min_values=1,
        max_values=1,
        custom_id="operator_panel_chain_select",
        options=[
            discord.SelectOption(label="EVM:sepolia", value="EVM:sepolia"),
            discord.SelectOption(label="EVM:base", value="EVM:base"),
            discord.SelectOption(label="EVM:amoy", value="EVM:amoy"),
            discord.SelectOption(label="EVM:mainnet", value="EVM:mainnet"),
        ],
    )
    async def chain_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._precheck(interaction, "chain"):
            return
        value = str(select.values[0]).strip() if select.values else "EVM:sepolia"
        try:
            out = await self.bot_ref._apply_action(actor=str(interaction.user), action="chain_set", value=value, reason="panel")
            await self._refresh_embed(interaction)
            await interaction.followup.send(f"ok chain={value} op_id={out['op_id']}", ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_chain instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Last10", style=discord.ButtonStyle.secondary, custom_id="operator_panel_last10")
    async def last10_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "last10"):
            return
        try:
            resp = await self.bot_ref._api_get("/attempts", params={"limit": 10})
            rows = resp.get("items", []) if isinstance(resp, dict) else []
            _, payload = await self.bot_ref._status_payload()
            chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
            em = ui.build_last_embed(items=rows, limit=10, instance_id=self.bot_ref.instance_id, chain=chain)
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_last10 instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Top", style=discord.ButtonStyle.secondary, custom_id="operator_panel_top")
    async def top_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "top"):
            return
        try:
            resp = await self.bot_ref._api_get("/top", params={"window": "24h"})
            rows = resp.get("items", []) if isinstance(resp, dict) else []
            _, payload = await self.bot_ref._status_payload()
            chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
            em = ui.build_top_embed(window="24h", items=rows, instance_id=self.bot_ref.instance_id, chain=chain)
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_top instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Readiness", style=discord.ButtonStyle.secondary, custom_id="operator_panel_readiness")
    async def readiness_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "readiness"):
            return
        try:
            resp = await self.bot_ref._api_get("/readiness")
            _, payload = await self.bot_ref._status_payload()
            chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
            em = ui.build_readiness_embed(payload=resp, instance_id=self.bot_ref.instance_id, chain=chain)
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_readiness instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    @discord.ui.button(label="Pipeline", style=discord.ButtonStyle.secondary, custom_id="operator_panel_pipeline")
    async def pipeline_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._precheck(interaction, "pipeline"):
            return
        try:
            resp = await self.bot_ref._api_get("/pipeline")
            _, payload = await self.bot_ref._status_payload()
            chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
            em = ui.build_pipeline_embed(resp, instance_id=self.bot_ref.instance_id, chain=chain)
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:
            self.bot_ref.last_error = str(e)
            log.exception("interaction_error action=panel_pipeline instance_id=%s", self.bot_ref.instance_id)
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)


class OperatorBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=os.getenv("DISCORD_OPERATOR_PREFIX", "!"), intents=intents)
        self.api_base = os.getenv("MEVBOT_API_URL", os.getenv("DISCORD_OPERATOR_API_BASE", "http://mev-bot:8000")).rstrip("/")
        self.database_url = str(os.getenv("DATABASE_URL", "")).strip()
        self.guild_id = int(os.getenv("DISCORD_OPERATOR_GUILD_ID", "0") or "0")
        self.audit_channel_id = int(os.getenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "0") or "0")
        self.command_channel_id = int(os.getenv("DISCORD_OPERATOR_COMMAND_CHANNEL_ID", "0") or "0")
        self.allowed_channel_ids = {
            int(x.strip())
            for x in str(os.getenv("DISCORD_OPERATOR_ALLOWED_CHANNEL_IDS", "")).split(",")
            if x.strip().isdigit()
        }
        self.allowed_user_ids = {
            int(x.strip())
            for x in str(os.getenv("DISCORD_OPERATOR_ALLOWED_USER_IDS", "")).split(",")
            if x.strip().isdigit()
        }
        self.allowed_role_ids = {
            int(x.strip())
            for x in str(os.getenv("DISCORD_OPERATOR_ALLOWED_ROLE_IDS", "")).split(",")
            if x.strip().isdigit()
        }
        self.confirm_ttl_s = int(os.getenv("DISCORD_OPERATOR_CONFIRM_TTL_S", "120"))
        self.status_channel_id = int(os.getenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", "0") or "0")
        self.status_refresh_s = int(os.getenv("DISCORD_OPERATOR_STATUS_REFRESH_S", "45"))
        self.instance_id = os.getenv("BOT_INSTANCE_ID", secrets.token_hex(4))
        self.operator_impl = "ops.discord_operator"
        self.sync_mode = "global"
        self.last_error: str = ""
        self.operator_owner_id = int(_read_ops_value("discord_operator_owner_id", "0") or "0")
        self.panel_message_id: int = 0
        self.panel_channel_id: int = 0
        self.panel_view: Optional[OperatorPanelView] = None
        self.pending: Dict[str, PendingConfirmation] = {}
        self.api_http = httpx.AsyncClient(timeout=8.0)
        self.status_card = StatusCardManager(
            bot=self,
            status_channel_id=self.status_channel_id,
            refresh_s=self.status_refresh_s,
            snapshot_fetcher=self._status_card_snapshot,
            embed_fetcher=self._status_card_embed,
            kv_read=_read_ops_value,
            kv_write=_write_ops_value,
            audit_fn=self._audit,
        )
        self._heartbeat_task: Optional[asyncio.Task] = None
        mode_env = str(os.getenv("MODE", "paper")).strip().lower()
        if (
            self.operator_owner_id <= 0
            and not self.allowed_user_ids
            and not self.allowed_role_ids
            and mode_env in {"development", "dev", "paper", "dryrun", "test"}
        ):
            default_owner = int(os.getenv("DISCORD_OPERATOR_DEFAULT_OWNER_ID", "798402164800225281") or "0")
            if default_owner > 0:
                self._set_operator_owner(default_owner)
                log.info("operator default owner enabled user_id=%s mode=%s", default_owner, mode_env)
        if not self.allowed_user_ids and not self.allowed_role_ids and not self.allowed_channel_ids:
            log.warning(
                "operator auth policy: no explicit allowlists set; defaulting to administrators or panel owner"
            )

    async def setup_hook(self) -> None:
        # Register trading visibility slash commands before command registry/sync.
        try:
            from ops.discord_commands_trading import setup as setup_trading

            await setup_trading(self, self.database_url)
            log.info("registered trading commands cog db_url_set=%s", bool(self.database_url))
        except Exception as e:
            log.exception("failed registering trading commands cog err=%s", e)
            raise

        if self.panel_view is None:
            self.panel_view = OperatorPanelView(self)
        # Log command registry once at startup and detect accidental duplicate names.
        root_cmds = self.tree.get_commands()
        names = [getattr(c, "name", "?") for c in root_cmds]
        dupes = sorted({n for n in names if names.count(n) > 1})
        log.info(
            "startup command registry count=%s names=%s duplicates=%s",
            len(names),
            ",".join(sorted(names)),
            ",".join(dupes) if dupes else "none",
        )
        if dupes:
            raise RuntimeError(f"duplicate slash command registration detected: {','.join(dupes)}")
        try:
            if self.guild_id > 0:
                guild_obj = discord.Object(id=self.guild_id)
                # Guild-mode must not publish global commands.
                self.tree.clear_commands(guild=guild_obj)
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                self.sync_mode = "guild"
                if len(synced) < 10:
                    raise RuntimeError(
                        f"slash sync returned too few commands for guild scope: count={len(synced)} expected>=10; check DISCORD_OPERATOR_GUILD_ID/app permissions"
                    )
                log.info(
                    "slash commands synced to guild id=%s count=%s sync_mode=%s operator_impl=%s instance_id=%s",
                    self.guild_id,
                    len(synced),
                    self.sync_mode,
                    self.operator_impl,
                    self.instance_id,
                )
                log.info(
                    "slash command list sync_mode=%s count=%s names=%s",
                    self.sync_mode,
                    len(synced),
                    ",".join(sorted(getattr(c, "name", "?") for c in synced)),
                )
            else:
                synced = await self.tree.sync()
                self.sync_mode = "global"
                if len(synced) < 10:
                    raise RuntimeError(
                        f"slash sync returned too few commands for global scope: count={len(synced)} expected>=10; verify bot application command scope"
                    )
                log.info(
                    "slash commands synced globally count=%s sync_mode=%s operator_impl=%s instance_id=%s",
                    len(synced),
                    self.sync_mode,
                    self.operator_impl,
                    self.instance_id,
                )
                log.info(
                    "slash command list sync_mode=%s count=%s names=%s",
                    self.sync_mode,
                    len(synced),
                    ",".join(sorted(getattr(c, "name", "?") for c in synced)),
                )
        except Exception as e:
            log.exception("slash command sync failed hard: %s", e)
            raise

    async def close(self) -> None:
        await self.status_card.stop()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
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
        await self._register_persistent_views()
        await self._restore_panel_from_state()
        self.status_card.start()
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                st = await self._api_get("/status")
                log.info(
                    "operator_heartbeat instance_id=%s api_base=%s desired=%s effective=%s last_error=%s",
                    self.instance_id,
                    self.api_base,
                    st.get("desired_state"),
                    st.get("effective_state"),
                    self.last_error or "none",
                )
            except Exception as e:
                self.last_error = str(e)
                log.warning("operator_heartbeat_failed instance_id=%s err=%s", self.instance_id, e)
            await asyncio.sleep(60)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        command_name = ""
        if interaction.type == discord.InteractionType.application_command:
            command_name = interaction.data.get("name", "") if isinstance(interaction.data, dict) else ""
        elif interaction.type == discord.InteractionType.component:
            command_name = interaction.data.get("custom_id", "") if isinstance(interaction.data, dict) else ""
        log.info(
            "interaction_received type=%s command_or_custom_id=%s user_id=%s channel_id=%s instance_id=%s",
            int(interaction.type.value) if interaction.type else -1,
            command_name or "unknown",
            interaction.user.id if interaction.user else 0,
            interaction.channel_id,
            self.instance_id,
        )
        try:
            # discord.py dispatches slash/component handlers before this event.
            # Keep this hook observational + stale-panel fallback only.
            if interaction.type == discord.InteractionType.component:
                if (
                    command_name.startswith("operator_panel_")
                    and self.panel_message_id
                    and interaction.message is not None
                    and int(interaction.message.id) != int(self.panel_message_id)
                    and not interaction.response.is_done()
                ):
                    await interaction.response.send_message(
                        "This panel is stale. Run /panel to refresh.",
                        ephemeral=True,
                    )
        except Exception as e:
            self.last_error = "interaction_dispatch_failed"
            log.exception(
                "interaction_exception command_or_custom_id=%s user_id=%s channel_id=%s instance_id=%s",
                command_name or "unknown",
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                self.instance_id,
            )
            with contextlib.suppress(Exception):
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Command failed: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Command failed: {e}", ephemeral=True)
        finally:
            log.info(
                "interaction_result command_or_custom_id=%s user_id=%s channel_id=%s instance_id=%s responded=%s",
                command_name or "unknown",
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                self.instance_id,
                bool(interaction.response.is_done()),
            )

    async def _register_persistent_views(self) -> None:
        if self.panel_view is None:
            self.panel_view = OperatorPanelView(self)
        self.add_view(self.panel_view)
        file_state = _load_panel_state_file()
        raw = str(os.getenv("DISCORD_OPERATOR_PANEL_MESSAGE_ID", "")).strip()
        if not raw:
            raw = str(file_state.get("panel_message_id", "")).strip()
        if not raw:
            raw = _read_ops_value("discord_operator_panel_message_id", "")
        try:
            self.panel_message_id = int(str(raw).strip()) if str(raw).strip() else 0
        except Exception:
            self.panel_message_id = 0
        raw_ch = str(file_state.get("panel_channel_id", "")).strip()
        try:
            self.panel_channel_id = int(raw_ch) if raw_ch else self.command_channel_id
        except Exception:
            self.panel_channel_id = self.command_channel_id
        if self.panel_message_id > 0:
            self.add_view(self.panel_view, message_id=self.panel_message_id)
            log.info(
                "panel view re-registered message_id=%s channel_id=%s instance_id=%s",
                self.panel_message_id,
                self.panel_channel_id,
                self.instance_id,
            )

    async def _restore_panel_from_state(self) -> None:
        if self.panel_view is None:
            self.panel_view = OperatorPanelView(self)
        if self.panel_message_id <= 0:
            return
        channel_id = self.panel_channel_id or self.command_channel_id
        if channel_id <= 0:
            return
        ch = self.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            with contextlib.suppress(Exception):
                fetched = await self.fetch_channel(channel_id)
                if isinstance(fetched, discord.TextChannel):
                    ch = fetched
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            msg = await ch.fetch_message(self.panel_message_id)
            _, payload = await self._status_payload()
            await msg.edit(embed=self._build_panel_embed(payload), view=self.panel_view)
            log.info(
                "panel restore ok message_id=%s channel_id=%s instance_id=%s",
                self.panel_message_id,
                channel_id,
                self.instance_id,
            )
        except Exception:
            log.warning(
                "panel restore failed message_id=%s channel_id=%s; run /panel to recreate",
                self.panel_message_id,
                channel_id,
            )
            self.panel_message_id = 0
            self.panel_channel_id = 0
            self._persist_panel_state()

    def _persist_panel_state(self) -> None:
        _write_ops_value("discord_operator_panel_message_id", str(self.panel_message_id or ""))
        _write_ops_value("discord_operator_panel_channel_id", str(self.panel_channel_id or ""))
        with contextlib.suppress(Exception):
            _save_panel_state_file(
                panel_message_id=int(self.panel_message_id or 0),
                panel_channel_id=int(self.panel_channel_id or 0),
            )

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        base = f"{self.api_base}{clean}"
        if params:
            return f"{base}?{urlencode(params, doseq=True)}"
        return base

    async def _check_channel(self, ctx: commands.Context) -> bool:
        channel_id = int(getattr(ctx.channel, "id", 0) or 0)
        ok_chan, chan_reason = self._channel_allowed(channel_id)
        if not ok_chan:
            await ctx.reply(f"not authorized: {chan_reason}")
            return False
        ok, reason = self._authorize_member(ctx.author, channel_id)
        if not ok:
            await ctx.reply(f"not authorized: {reason}")
            with contextlib.suppress(Exception):
                await self._audit(
                    f"operator_action_denied actor={ctx.author} command=prefix reason={reason} policy={self._auth_policy_summary()}"
                )
            return False
        return True

    def _channel_allowed(self, channel_id: int) -> tuple[bool, str]:
        # Backward-compatible single-channel restriction.
        if self.command_channel_id and channel_id != self.command_channel_id:
            return False, f"wrong_channel expected={self.command_channel_id} got={channel_id}"
        if self.allowed_channel_ids and channel_id not in self.allowed_channel_ids:
            return False, f"channel_not_allowed allowed={sorted(self.allowed_channel_ids)} got={channel_id}"
        return True, "ok"

    def _member_is_admin(self, member: Any) -> bool:
        return bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))

    def _auth_policy_summary(self) -> str:
        return (
            f"allowed_users={sorted(self.allowed_user_ids) if self.allowed_user_ids else 'unset'} "
            f"allowed_roles={sorted(self.allowed_role_ids) if self.allowed_role_ids else 'unset'} "
            f"allowed_channels={sorted(self.allowed_channel_ids) if self.allowed_channel_ids else 'unset'} "
            f"owner_id={self.operator_owner_id or 0} "
            f"default_policy=admin_or_owner_when_no_allowlists"
        )

    def _set_operator_owner(self, user_id: int) -> None:
        uid = int(user_id or 0)
        if uid <= 0:
            return
        self.operator_owner_id = uid
        _write_ops_value("discord_operator_owner_id", str(uid))

    def _authorize_member(self, member: Any, channel_id: int) -> tuple[bool, str]:
        ok_chan, chan_reason = self._channel_allowed(int(channel_id or 0))
        if not ok_chan:
            return False, chan_reason

        uid = int(getattr(member, "id", 0) or 0)
        # Explicit precedence: users > roles > open/admin-owner fallback.
        if self.allowed_user_ids:
            if uid in self.allowed_user_ids:
                return True, "allowed_user"
            return False, f"user_not_allowed user_id={uid}"

        if self.allowed_role_ids:
            roles = getattr(member, "roles", []) if member is not None else []
            role_ids = {int(getattr(r, "id", 0)) for r in roles}
            if role_ids & self.allowed_role_ids:
                return True, "allowed_role"
            return False, f"role_not_allowed roles={sorted(role_ids)}"

        # Safe dev default: allow server admins, or panel owner.
        if self._member_is_admin(member):
            return True, "allowed_admin_default"
        if self.operator_owner_id and uid == self.operator_owner_id:
            return True, "allowed_owner_default"
        return False, "not_admin_or_owner_default_policy"

    async def _authorize_interaction(self, interaction: discord.Interaction, command_name: str) -> tuple[bool, str]:
        uid = int(interaction.user.id) if interaction.user else 0
        # Bootstrap path for dev usability: first /panel claimant becomes owner when no explicit allowlists configured.
        if (
            command_name in {"panel", "panel:refresh"}
            and not self.allowed_user_ids
            and not self.allowed_role_ids
            and not self.operator_owner_id
        ):
            ok_chan, chan_reason = self._channel_allowed(int(interaction.channel_id or 0))
            if ok_chan:
                self._set_operator_owner(uid)
                await self._audit(
                    f"operator_owner_bootstrap actor={interaction.user} user_id={uid} reason=first_panel_claim"
                )
                return True, "owner_bootstrap_claim"
            return False, chan_reason

        ok, reason = self._authorize_member(interaction.user, int(interaction.channel_id or 0))
        if not ok:
            log.warning(
                "interaction_denied command=%s user_id=%s channel_id=%s instance_id=%s reason=%s policy=%s",
                command_name,
                uid,
                interaction.channel_id,
                self.instance_id,
                reason,
                self._auth_policy_summary(),
            )
            await self._audit(
                f"operator_action_denied actor={interaction.user} command={command_name} reason={reason} policy={self._auth_policy_summary()}"
            )
        return ok, reason

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

    def _build_panel_embed(self, payload: dict) -> discord.Embed:
        return ui.build_operator_status_embed(payload, instance_id=self.instance_id)

    async def _ensure_panel_message(self) -> discord.Message:
        if self.panel_view is None:
            self.panel_view = OperatorPanelView(self)
        if self.command_channel_id <= 0:
            raise RuntimeError("DISCORD_OPERATOR_COMMAND_CHANNEL_ID is required for /panel")
        ch = self.get_channel(self.command_channel_id)
        if not isinstance(ch, discord.TextChannel):
            fetched = await self.fetch_channel(self.command_channel_id)
            if not isinstance(fetched, discord.TextChannel):
                raise RuntimeError("operator command channel is not a text channel")
            ch = fetched

        env_id = str(os.getenv("DISCORD_OPERATOR_PANEL_MESSAGE_ID", "")).strip()
        if env_id:
            with contextlib.suppress(Exception):
                self.panel_message_id = int(env_id)
                self.panel_channel_id = self.command_channel_id
        if self.panel_message_id <= 0:
            raw = _read_ops_value("discord_operator_panel_message_id", "")
            with contextlib.suppress(Exception):
                self.panel_message_id = int(raw)
            raw_ch = _read_ops_value("discord_operator_panel_channel_id", "")
            with contextlib.suppress(Exception):
                self.panel_channel_id = int(raw_ch) if str(raw_ch).strip() else self.command_channel_id

        if self.panel_message_id > 0:
            try:
                current_ch = ch
                if self.panel_channel_id and self.panel_channel_id != ch.id:
                    maybe_ch = self.get_channel(self.panel_channel_id)
                    if not isinstance(maybe_ch, discord.TextChannel):
                        fetched = await self.fetch_channel(self.panel_channel_id)
                        if isinstance(fetched, discord.TextChannel):
                            maybe_ch = fetched
                    if isinstance(maybe_ch, discord.TextChannel):
                        current_ch = maybe_ch
                msg = await current_ch.fetch_message(self.panel_message_id)
                return msg
            except Exception:
                self.panel_message_id = 0
                self.panel_channel_id = 0

        _, payload = await self._status_payload()
        embed = self._build_panel_embed(payload)
        msg = await ch.send(embed=embed, view=self.panel_view)
        with contextlib.suppress(Exception):
            await msg.pin(reason="MEV operator control panel")
        self.panel_message_id = int(msg.id)
        self.panel_channel_id = int(ch.id)
        self._persist_panel_state()
        self.add_view(self.panel_view, message_id=msg.id)
        await self._audit(f"operator_panel_created channel={self.command_channel_id} message_id={msg.id}")
        return msg

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

    async def _status_card_embed(self) -> discord.Embed:
        # Use the same payload model as /status command for status-channel updates.
        _, payload = await self._status_payload()
        return self._build_panel_embed(payload)

    async def _refresh_operator_surfaces(self) -> None:
        # Best-effort immediate refresh after operator actions.
        with contextlib.suppress(Exception):
            if self.panel_message_id > 0:
                channel_id = self.panel_channel_id or self.command_channel_id
                ch = self.get_channel(channel_id)
                if not isinstance(ch, discord.TextChannel):
                    fetched = await self.fetch_channel(channel_id)
                    if isinstance(fetched, discord.TextChannel):
                        ch = fetched
                if isinstance(ch, discord.TextChannel):
                    msg = await ch.fetch_message(self.panel_message_id)
                    _, payload = await self._status_payload()
                    await msg.edit(embed=self._build_panel_embed(payload), view=self.panel_view)
        with contextlib.suppress(Exception):
            await self.status_card.refresh_once()

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
        result_text = ""
        a = action.lower().strip()
        v = value.lower().strip()
        try:
            if a == "pause":
                out = await self._api_post("/operator/pause")
                result_text = str(out.get("message", "paused"))
                op_id = str(out.get("op_id", secrets.token_hex(12)))
            elif a == "resume":
                out = await self._api_post("/operator/resume")
                result_text = str(out.get("message", "resumed"))
                op_id = str(out.get("op_id", secrets.token_hex(12)))
            elif a == "mode":
                if v not in {"dryrun", "paper", "live"}:
                    raise ValueError("mode must be one of: dryrun|paper|live")
                out = await self._api_post("/operator/mode", json={"mode": v})
                result_text = str(out.get("message", f"mode={v}"))
                op_id = str(out.get("op_id", secrets.token_hex(12)))
            elif a == "kill_switch":
                if v in {"on", "true", "1"}:
                    out = await self._api_post("/operator/killswitch", json={"enabled": True})
                    result_text = str(out.get("message", "kill_switch=on"))
                    op_id = str(out.get("op_id", secrets.token_hex(12)))
                elif v in {"off", "false", "0"}:
                    out = await self._api_post("/operator/killswitch", json={"enabled": False})
                    result_text = str(out.get("message", "kill_switch=off"))
                    op_id = str(out.get("op_id", secrets.token_hex(12)))
                else:
                    raise ValueError("kill-switch must be on|off")
            elif a == "chain_set":
                selection = parse_chain_selection(value)
                selection_name = f"{selection.family}:{selection.chain}"
                out = await self._api_post("/operator/chain", json={"chain_key": selection_name})
                result_text = str(out.get("message", f"desired_chain={selection_name}"))
                op_id = str(out.get("op_id", secrets.token_hex(12)))
            else:
                raise ValueError(f"unknown action: {action}")
            await self._audit(
                f"operator_action op_id={op_id} actor={actor} action={a} value={v} reason={reason} result=success"
            )
            await self._refresh_operator_surfaces()
            return {"op_id": op_id, "result": result_text}
        except Exception as e:
            await self._audit(
                f"operator_action actor={actor} action={a} value={v} reason={reason} result=fail err={e}"
            )
            raise


def _build_bot() -> OperatorBot:
    bot = OperatorBot()
    def _response(*, content: str | None = None, embed: discord.Embed | None = None) -> dict[str, Any]:
        return {"content": content, "embed": embed}

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

    async def handle_status() -> dict[str, Any]:
        _, payload = await bot._status_payload()
        return _response(embed=ui.build_operator_status_embed(payload, instance_id=bot.instance_id))

    async def handle_help() -> dict[str, Any]:
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        return _response(embed=ui.build_help_embed(instance_id=bot.instance_id, chain=chain))

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

    async def handle_ops(limit: int = 20) -> dict[str, Any]:
        resp = await bot._api_get("/operator/events", params={"limit": max(1, min(int(limit), 50))})
        rows = resp.get("items", []) if isinstance(resp, dict) else []
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        if not rows:
            em = discord.Embed(title="✅ Operator Actions", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
            em.description = "No operator actions found."
            em.set_footer(text=ui.footer(bot.instance_id, chain))
            return _response(embed=em)
        lines = []
        for row in rows[:20]:
            op_id = str(row.get("op_id", "—"))
            action = str(row.get("action", "unknown"))
            applied = bool(row.get("applied", False))
            err = str(row.get("error", "") or "—")
            ts = str(row.get("ts", "") or row.get("created_at", "") or "—")
            icon = "✅" if applied else "🟥"
            lines.append(f"{icon} `{ui.fmt_ts(ts)}` `{op_id}` `{action}` err={err}")
        em = discord.Embed(title="✅ Operator Actions", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        em.description = ui.compact_table_lines(lines, limit=20, max_chars=3500)
        em.set_footer(text=ui.footer(bot.instance_id, chain))
        return _response(embed=em)

    async def handle_last(limit: int = 10) -> dict[str, Any]:
        resp = await bot._api_get("/attempts", params={"limit": max(1, min(int(limit), 50))})
        rows = resp.get("items", []) if isinstance(resp, dict) else []
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        em = ui.build_last_embed(
            items=rows,
            limit=max(1, min(int(limit), 50)),
            instance_id=bot.instance_id,
            chain=chain,
        )
        return _response(embed=em)

    async def handle_top(window: str = "24h") -> dict[str, Any]:
        resp = await bot._api_get("/top", params={"window": window})
        rows = resp.get("items", []) if isinstance(resp, dict) else []
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        return _response(embed=ui.build_top_embed(window=window, items=rows, instance_id=bot.instance_id, chain=chain))

    async def handle_pipeline() -> dict[str, Any]:
        resp = await bot._api_get("/pipeline")
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        return _response(embed=ui.build_pipeline_embed(resp, instance_id=bot.instance_id, chain=chain))

    async def handle_strategies() -> str:
        resp = await bot._api_get("/strategies")
        rows = resp.get("items", []) if isinstance(resp, dict) else []
        if not rows:
            return "No strategies configured."
        return "\n".join(
            f"{r.get('name')}: seen={r.get('seen_10m',0)} selected={r.get('selected_10m',0)} sim_fail={r.get('sim_fail_10m',0)}"
            for r in rows[:20]
        )

    async def handle_readiness() -> dict[str, Any]:
        resp = await bot._api_get("/readiness")
        _, payload = await bot._status_payload()
        chain = str(payload.get("effective_chain") or payload.get("chain") or "unknown")
        return _response(embed=ui.build_readiness_embed(payload=resp, instance_id=bot.instance_id, chain=chain))

    async def handle_report(window: str = "24h") -> dict[str, Any]:
        s = await bot._api_get("/status")
        t = await bot._api_get("/top", params={"window": window})
        a = await bot._api_get("/attempts", params={"limit": 10})
        top_items = t.get("items", []) if isinstance(t, dict) else []
        attempts = a.get("items", []) if isinstance(a, dict) else []
        chain = str(s.get("effective_chain") or s.get("chain") or "unknown")
        return _response(
            embed=ui.build_report_embed(
                window=window,
                status=s,
                top_items=top_items,
                attempts_count=len(attempts),
                instance_id=bot.instance_id,
                chain=chain,
            )
        )

    async def _slash_exec(
        interaction: discord.Interaction,
        handler,
        *,
        ephemeral: bool = True,
    ) -> None:
        responded = False
        cmd_name = interaction.command.name if interaction.command else "unknown"
        try:
            start_ts = time.time()
            await interaction.response.defer(ephemeral=ephemeral)
            responded = True
            ok, reason = await bot._authorize_interaction(interaction, cmd_name)
            if not ok:
                await interaction.followup.send(
                    embed=ui.build_not_authorized_embed(
                        reason=reason,
                        policy=bot._auth_policy_summary(),
                        instance_id=bot.instance_id,
                    ),
                    ephemeral=True,
                )
                log.info("slash_denied command=%s user_id=%s channel_id=%s instance_id=%s responded=true", cmd_name, interaction.user.id if interaction.user else 0, interaction.channel_id, bot.instance_id)
                return
            result = await handler()
            if isinstance(result, dict):
                embed = result.get("embed")
                content = result.get("content")
                await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            elif isinstance(result, discord.Embed):
                await interaction.followup.send(embed=result, ephemeral=ephemeral)
            else:
                await interaction.followup.send(f"{result}\ninstance_id={bot.instance_id}", ephemeral=ephemeral)
            log.info(
                "slash_ok command=%s user_id=%s channel_id=%s instance_id=%s responded=true elapsed_ms=%s",
                cmd_name,
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                bot.instance_id,
                int((time.time() - start_ts) * 1000),
            )
        except Exception as e:
            bot.last_error = str(e)
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

    @bot.tree.command(name="help", description="Show operator command help")
    async def slash_help(interaction: discord.Interaction):
        await _slash_exec(interaction, handle_help, ephemeral=True)

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
                prep = await bot._api_post("/operator/live/prepare")
                return f"live_mode_prepare token={prep.get('token','—')} expires_in_s={prep.get('expires_in_s',60)} then run /confirm_live"
            return await handle_mode(actor=str(interaction.user), value=value, reason="slash")
        await _slash_exec(interaction, _handler, ephemeral=True)

    @bot.tree.command(name="confirm_live", description="Confirm live mode with token")
    @app_commands.describe(token="Token returned by /mode live prepare")
    async def slash_confirm_live(interaction: discord.Interaction, token: str):
        async def _handler() -> str:
            out = await bot._api_post("/operator/live/commit", json={"token": token.strip()})
            return f"{out.get('message','live commit')}\nop_id={out.get('op_id','—')}"
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

    @bot.tree.command(name="last", description="Show last attempts")
    @app_commands.describe(n="Number of attempts (1-50)")
    async def slash_last(interaction: discord.Interaction, n: int = 10):
        await _slash_exec(interaction, lambda: handle_last(limit=max(1, min(int(n), 50))), ephemeral=True)

    @bot.tree.command(name="top", description="Top reject reasons")
    @app_commands.describe(window="Window like 24h or 10m")
    async def slash_top(interaction: discord.Interaction, window: str = "24h"):
        await _slash_exec(interaction, lambda: handle_top(window=window), ephemeral=True)

    @bot.tree.command(name="pipeline", description="Pipeline diagnostics")
    async def slash_pipeline(interaction: discord.Interaction):
        await _slash_exec(interaction, handle_pipeline, ephemeral=True)

    @bot.tree.command(name="strategies", description="Strategies diagnostics")
    async def slash_strategies(interaction: discord.Interaction):
        await _slash_exec(interaction, handle_strategies, ephemeral=True)

    @bot.tree.command(name="readiness", description="Readiness checks")
    async def slash_readiness(interaction: discord.Interaction):
        await _slash_exec(interaction, handle_readiness, ephemeral=True)

    @bot.tree.command(name="report", description="Post concise operator KPI report")
    @app_commands.describe(window="Window like 24h or 10m")
    async def slash_report(interaction: discord.Interaction, window: str = "24h"):
        await _slash_exec(interaction, lambda: handle_report(window=window), ephemeral=True)

    @bot.tree.command(name="killswitch", description="Set kill switch on/off")
    @app_commands.describe(value="on or off")
    async def slash_killswitch(interaction: discord.Interaction, value: str):
        await _slash_exec(interaction, lambda: handle_kill(actor=str(interaction.user), value=value, reason="slash"), ephemeral=True)

    @bot.tree.command(name="ping", description="Operator bot health ping")
    async def slash_ping(interaction: discord.Interaction):
        async def _handler() -> str:
            db_ok = False
            api_ok = False
            with contextlib.suppress(Exception):
                with psycopg.connect(_ops_dsn(), autocommit=True) as conn:
                    conn.execute("SELECT 1")
                    db_ok = True
            with contextlib.suppress(Exception):
                h = await bot._api_get("/health")
                api_ok = bool(h.get("ok"))
            return (
                f"pong instance_id={bot.instance_id} api_base={bot.api_base} sync={bot.sync_mode} "
                f"db_ok={db_ok} api_ok={api_ok} last_error={bot.last_error or 'none'}"
            )
        await _slash_exec(interaction, _handler, ephemeral=True)

    @bot.tree.command(name="diag", description="Operator diagnostics")
    @app_commands.describe(action="status | auth | clear_global_commands")
    async def slash_diag(interaction: discord.Interaction, action: str = "status"):
        async def _handler() -> str:
            db_ok = False
            dsn = _ops_dsn()
            if dsn:
                with contextlib.suppress(Exception):
                    with psycopg.connect(dsn, autocommit=True) as conn:
                        conn.execute("SELECT 1")
                        db_ok = True
            missing: list[str] = []
            perms_summary = "n/a"
            ch = interaction.channel
            if isinstance(ch, discord.abc.GuildChannel) and interaction.guild and interaction.guild.me:
                perms = ch.permissions_for(interaction.guild.me)
                checks = {
                    "send_messages": bool(getattr(perms, "send_messages", False)),
                    "embed_links": bool(getattr(perms, "embed_links", False)),
                    "read_message_history": bool(getattr(perms, "read_message_history", False)),
                    "use_application_commands": bool(getattr(perms, "use_application_commands", False)),
                }
                missing = [k for k, ok in checks.items() if not ok]
                perms_summary = ",".join([k for k, ok in checks.items() if ok]) or "none"
            act = str(action).strip().lower()
            if act == "clear_global_commands":
                member = interaction.user
                is_admin = bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))
                if not is_admin:
                    return "not authorized: admin required for clear_global_commands"
                self_tree = bot.tree
                self_tree.clear_commands(guild=None)
                global_synced = await self_tree.sync()
                if bot.guild_id > 0:
                    guild_obj = discord.Object(id=bot.guild_id)
                    self_tree.clear_commands(guild=guild_obj)
                    self_tree.copy_global_to(guild=guild_obj)
                    guild_synced = await self_tree.sync(guild=guild_obj)
                else:
                    guild_synced = []
                return (
                    f"clear_global_commands done global_count={len(global_synced)} "
                    f"guild_count={len(guild_synced)}"
                )
            if act == "auth":
                member = interaction.user
                uid = int(getattr(member, "id", 0) or 0)
                roles = getattr(member, "roles", []) if member is not None else []
                role_ids = sorted({int(getattr(r, "id", 0)) for r in roles if int(getattr(r, "id", 0)) > 0})
                ok, reason = bot._authorize_member(member, int(interaction.channel_id or 0))
                return (
                    f"auth allowed={ok} reason={reason} "
                    f"user_id={uid} roles={role_ids or 'none'} "
                    f"policy={bot._auth_policy_summary()}"
                )
            return (
                f"diag instance_id={bot.instance_id} api_base={bot.api_base} "
                f"db_ok={db_ok} missing_perms={','.join(missing) if missing else 'none'} "
                f"ok_perms={perms_summary} last_error={bot.last_error or 'none'}"
            )
        await _slash_exec(interaction, _handler, ephemeral=True)

    @bot.tree.command(name="panel", description="Create or refresh operator control panel")
    async def slash_panel(interaction: discord.Interaction):
        cmd_name = interaction.command.name if interaction.command else "panel"
        try:
            await interaction.response.defer(ephemeral=True)
            ok, reason = await bot._authorize_interaction(interaction, cmd_name)
            if not ok:
                await interaction.followup.send(
                    embed=ui.build_not_authorized_embed(
                        reason=reason,
                        policy=bot._auth_policy_summary(),
                        instance_id=bot.instance_id,
                    ),
                    ephemeral=True,
                )
                return
            # Persist panel owner for default admin/owner auth policy (when no explicit allowlists).
            if not bot.allowed_user_ids and not bot.allowed_role_ids:
                with contextlib.suppress(Exception):
                    bot._set_operator_owner(int(interaction.user.id if interaction.user else 0))
            msg = await bot._ensure_panel_message()
            _, payload = await bot._status_payload()
            embed = bot._build_panel_embed(payload)
            with contextlib.suppress(Exception):
                chains = await bot._api_get("/chains")
                items = chains.get("items", []) if isinstance(chains, dict) else []
                opts = []
                seen_vals = set()
                for it in items:
                    key = str(it.get("key", "")).strip()
                    if not key or key in seen_vals:
                        continue
                    seen_vals.add(key)
                    opts.append(discord.SelectOption(label=key, value=key))
                if opts and bot.panel_view is not None:
                    for child in bot.panel_view.children:
                        if isinstance(child, discord.ui.Select) and str(getattr(child, "custom_id", "")) == "operator_panel_chain_select":
                            child.options = opts[:25]
            await msg.edit(embed=embed, view=bot.panel_view)
            if bot.panel_message_id != msg.id:
                bot.panel_message_id = int(msg.id)
            bot.panel_channel_id = int(getattr(msg.channel, "id", bot.command_channel_id) or bot.command_channel_id)
            bot._persist_panel_state()
            await interaction.followup.send(f"ok panel_ready message_id={msg.id}", ephemeral=True)
            log.info(
                "slash_ok command=panel user_id=%s channel_id=%s instance_id=%s responded=true",
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                bot.instance_id,
            )
        except Exception as e:
            bot.last_error = str(e)
            log.exception(
                "slash_error command=panel user_id=%s channel_id=%s instance_id=%s",
                interaction.user.id if interaction.user else 0,
                interaction.channel_id,
                bot.instance_id,
            )
            with contextlib.suppress(Exception):
                await interaction.followup.send(f"Command failed: {e}", ephemeral=True)

    return bot


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    token = os.getenv("DISCORD_OPERATOR_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_OPERATOR_TOKEN is required for discord operator bot")
    bot = _build_bot()
    log.info(
        "operator starting operator_impl=%s instance_id=%s api_base=%s guild_id=%s command_channel_id=%s status_channel_id=%s audit_channel_id=%s auth_policy=%s",
        bot.operator_impl,
        bot.instance_id,
        bot.api_base,
        bot.guild_id if bot.guild_id > 0 else "global",
        bot.command_channel_id,
        bot.status_channel_id,
        bot.audit_channel_id,
        bot._auth_policy_summary(),
    )
    bot.run(token)


if __name__ == "__main__":
    main()

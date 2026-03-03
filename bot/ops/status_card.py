from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import discord

log = logging.getLogger("discord-status-card")


@dataclass
class StatusCardSnapshot:
    state: str
    mode: str
    chain_family: str
    chain: str
    rpc_health: str
    last_trade_time: str
    today_pnl: str
    error_rate: str
    updated_at: str


class StatusCardManager:
    def __init__(
        self,
        *,
        bot: discord.Client,
        status_channel_id: int,
        refresh_s: int,
        snapshot_fetcher: Callable[[], Awaitable[StatusCardSnapshot]],
        kv_read: Callable[[str, str], str],
        kv_write: Callable[[str, str], None],
        audit_fn: Callable[[str], Awaitable[None]],
    ) -> None:
        self.bot = bot
        self.status_channel_id = status_channel_id
        self.refresh_s = max(30, min(refresh_s, 60))
        self.snapshot_fetcher = snapshot_fetcher
        self.kv_read = kv_read
        self.kv_write = kv_write
        self.audit_fn = audit_fn
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self.status_channel_id <= 0:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception as e:
                log.warning("status card refresh failed: %s", e)
            await asyncio.sleep(self.refresh_s)

    async def refresh_once(self) -> None:
        ch = await self._resolve_channel()
        if ch is None:
            return
        snap = await self.snapshot_fetcher()
        embed = self._build_embed(snap)

        msg = await self._resolve_message(ch)
        if msg is None:
            msg = await self._discord_call(ch.send, embed=embed)
            self.kv_write("discord_status_card_message_id", str(msg.id))
            await self._safe_pin(msg)
            await self.audit_fn(f"status_card_created channel={self.status_channel_id} message_id={msg.id}")
            return

        await self._discord_call(msg.edit, embed=embed)

    async def _resolve_channel(self) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(self.status_channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await self._discord_call(self.bot.fetch_channel, self.status_channel_id)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except Exception as e:
            log.warning("status channel fetch failed id=%s err=%s", self.status_channel_id, e)
        return None

    async def _resolve_message(self, ch: discord.TextChannel) -> Optional[discord.Message]:
        raw = self.kv_read("discord_status_card_message_id", "")
        if not raw:
            return None
        try:
            msg_id = int(raw)
        except Exception:
            return None
        try:
            return await self._discord_call(ch.fetch_message, msg_id)
        except discord.NotFound:
            self.kv_write("discord_status_card_message_id", "")
            await self.audit_fn(
                f"status_card_missing_recreate channel={self.status_channel_id} old_message_id={msg_id}"
            )
            return None

    async def _safe_pin(self, msg: discord.Message) -> None:
        try:
            await self._discord_call(msg.pin, reason="MEV operator status card")
        except Exception as e:
            log.warning("status card pin failed message_id=%s err=%s", msg.id, e)

    async def _discord_call(self, fn, *args, **kwargs):
        delay = 1.0
        for attempt in range(6):
            try:
                return await fn(*args, **kwargs)
            except discord.HTTPException as e:
                retry_after = float(getattr(e, "retry_after", 0.0) or 0.0)
                is_rate_limit = getattr(e, "status", None) == 429
                if not is_rate_limit and retry_after <= 0 and attempt >= 2:
                    raise
                sleep_s = retry_after if retry_after > 0 else delay
                await asyncio.sleep(min(max(sleep_s, 0.5), 15.0))
                delay = min(delay * 2.0, 15.0)
        raise RuntimeError("discord call retry budget exhausted")

    def _build_embed(self, s: StatusCardSnapshot) -> discord.Embed:
        em = discord.Embed(
            title="MEV Bot Status Card",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        em.add_field(name="State", value=s.state, inline=True)
        em.add_field(name="Mode", value=s.mode, inline=True)
        em.add_field(name="Chain", value=f"{s.chain_family}:{s.chain}", inline=True)
        em.add_field(name="RPC Health", value=s.rpc_health, inline=False)
        em.add_field(name="Last Trade", value=s.last_trade_time, inline=True)
        em.add_field(name="Today PnL", value=s.today_pnl, inline=True)
        em.add_field(name="Error Rate", value=s.error_rate, inline=True)
        em.set_footer(text=f"updated {s.updated_at}")
        return em


def fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None or not math.isfinite(v):
        return "n/a"
    return f"{v:.{digits}f}"

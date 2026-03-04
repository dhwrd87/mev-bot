from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import requests


OnSlotFn = Callable[[int, str], Awaitable[None]]


class SolSlotTracker:
    """Observe-only Solana slot tracker (polling-based)."""

    def __init__(self, *, endpoint: str, on_slot: OnSlotFn, poll_s: float = 2.0) -> None:
        self.endpoint = str(endpoint).strip()
        self.on_slot = on_slot
        self.poll_s = max(0.5, float(poll_s))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.current_slot: int = 0
        self.last_update_ts: float = 0.0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                slot = int(await asyncio.to_thread(self._get_slot))
                self.current_slot = slot
                self.last_update_ts = time.time()
                await self.on_slot(slot, self.endpoint)
            except Exception:
                # Observe-only tracker should not crash runtime loop.
                pass
            await asyncio.sleep(self.poll_s)

    def _get_slot(self) -> int:
        resp = requests.post(
            self.endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(body["error"])
        return int(body.get("result") or 0)

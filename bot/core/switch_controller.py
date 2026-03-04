from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


ApplyFn = Callable[[str], Awaitable[None]]
ValidateFn = Callable[[str], Awaitable[None]]


@dataclass
class SwitchSnapshot:
    desired_chain: str
    effective_chain: str
    switching_in_progress: bool
    last_transition_error: Optional[str]
    last_started_ts: float
    last_finished_ts: float


class SwitchController:
    def __init__(self) -> None:
        self.desired_chain: str = "UNKNOWN"
        self.effective_chain: str = "UNKNOWN"
        self.switching_in_progress: bool = False
        self.last_transition_error: Optional[str] = None
        self.last_started_ts: float = 0.0
        self.last_finished_ts: float = 0.0
        self._lock = asyncio.Lock()

    async def reconcile(self, *, desired_chain: str, effective_chain: str, apply_fn: ApplyFn, validate_fn: ValidateFn) -> bool:
        desired = str(desired_chain or "").strip()
        effective = str(effective_chain or "").strip()
        self.desired_chain = desired or "UNKNOWN"
        self.effective_chain = effective or "UNKNOWN"

        if not desired or desired == "UNKNOWN" or desired == effective:
            return False

        async with self._lock:
            # Re-check under lock in case another task already switched.
            if desired == self.effective_chain:
                return False
            self.switching_in_progress = True
            self.last_transition_error = None
            self.last_started_ts = time.time()
            try:
                await apply_fn(desired)
                await validate_fn(desired)
                self.effective_chain = desired
                return True
            except Exception as e:
                self.last_transition_error = str(e)
                raise
            finally:
                self.switching_in_progress = False
                self.last_finished_ts = time.time()

    def snapshot(self) -> SwitchSnapshot:
        return SwitchSnapshot(
            desired_chain=self.desired_chain,
            effective_chain=self.effective_chain,
            switching_in_progress=self.switching_in_progress,
            last_transition_error=self.last_transition_error,
            last_started_ts=self.last_started_ts,
            last_finished_ts=self.last_finished_ts,
        )

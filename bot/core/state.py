from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Set


class BotState(str, Enum):
    BOOTING = "BOOTING"
    SYNCING = "SYNCING"
    READY = "READY"
    TRADING = "TRADING"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    PANIC = "PANIC"


ALL_BOT_STATES = tuple(s.value for s in BotState)

_ALLOWED_TRANSITIONS: Dict[BotState, Set[BotState]] = {
    BotState.BOOTING: {BotState.SYNCING, BotState.PAUSED, BotState.PANIC},
    BotState.SYNCING: {BotState.READY, BotState.PAUSED, BotState.DEGRADED, BotState.PANIC},
    BotState.READY: {BotState.TRADING, BotState.PAUSED, BotState.DEGRADED, BotState.PANIC},
    BotState.TRADING: {BotState.READY, BotState.PAUSED, BotState.DEGRADED, BotState.PANIC},
    BotState.PAUSED: {BotState.READY, BotState.TRADING, BotState.SYNCING, BotState.PANIC},
    BotState.DEGRADED: {BotState.READY, BotState.PAUSED, BotState.PANIC},
    BotState.PANIC: {BotState.PAUSED, BotState.READY},
}


def _is_truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_bot_state(raw: str | BotState) -> BotState:
    if isinstance(raw, BotState):
        return raw
    value = str(raw or "").strip().upper()
    try:
        return BotState(value)
    except Exception as e:
        raise ValueError(f"invalid bot state '{raw}'") from e


@dataclass(frozen=True)
class StateTransition:
    ts_ms: int
    actor: str
    reason: str
    from_state: str
    to_state: str


@dataclass
class BotStateMachine:
    state: BotState = BotState.PAUSED
    lockdown: bool = False
    history: List[StateTransition] = field(default_factory=list)

    def transition(
        self,
        target: str | BotState,
        *,
        actor: str = "system",
        reason: str = "manual",
        force: bool = False,
    ) -> StateTransition:
        to_state = parse_bot_state(target)
        from_state = self.state
        if from_state == to_state:
            rec = StateTransition(
                ts_ms=now_ms(),
                actor=str(actor),
                reason=str(reason),
                from_state=from_state.value,
                to_state=to_state.value,
            )
            self.history.append(rec)
            return rec
        if self.lockdown and not force:
            raise ValueError("state transition blocked by BOT_STATE_LOCKDOWN")
        if to_state not in _ALLOWED_TRANSITIONS.get(from_state, set()) and not force:
            raise ValueError(f"invalid state transition: {from_state.value} -> {to_state.value}")
        self.state = to_state
        rec = StateTransition(
            ts_ms=now_ms(),
            actor=str(actor),
            reason=str(reason),
            from_state=from_state.value,
            to_state=to_state.value,
        )
        self.history.append(rec)
        return rec

    def pause(self, *, actor: str = "manual", reason: str = "pause") -> StateTransition:
        return self.transition(BotState.PAUSED, actor=actor, reason=reason)

    def resume(self, *, actor: str = "manual", reason: str = "resume_to_trading") -> StateTransition:
        return self.transition(BotState.TRADING, actor=actor, reason=reason)

    def trading_allowed(self) -> bool:
        return self.state == BotState.TRADING


def build_state_machine(initial: str | None = None) -> BotStateMachine:
    raw_state = initial if initial is not None else os.getenv("BOT_INITIAL_STATE", BotState.PAUSED.value)
    initial_state = parse_bot_state(raw_state)
    lockdown = _is_truthy(os.getenv("BOT_STATE_LOCKDOWN", "false"))
    return BotStateMachine(state=initial_state, lockdown=lockdown)


def set_runtime_state(state: str | BotState) -> None:
    os.environ["BOT_RUNTIME_STATE"] = parse_bot_state(state).value


def get_runtime_state(default: str | BotState = BotState.PAUSED) -> BotState:
    raw = os.getenv("BOT_RUNTIME_STATE")
    if raw:
        return parse_bot_state(raw)
    return parse_bot_state(default)

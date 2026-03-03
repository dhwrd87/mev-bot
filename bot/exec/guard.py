from __future__ import annotations

import os


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def guard_enabled() -> bool:
    return _truthy(os.getenv("FEATURE_EXEC_GUARD", "0"))


def execution_enabled() -> bool:
    return _truthy(os.getenv("FEATURE_EXEC_ENABLE", "0"))


def runtime_state() -> str:
    return str(os.getenv("BOT_RUNTIME_STATE") or os.getenv("BOT_STATE") or "READY").strip().upper()


def allowed_states() -> set[str]:
    raw = str(os.getenv("FEATURE_EXEC_ALLOWED_STATES", "TRADING")).strip()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def should_block_execution(scope: str = "exec") -> tuple[bool, str]:
    _ = scope
    if not guard_enabled():
        return False, "guard_disabled"
    if not execution_enabled():
        return True, "feature_exec_enable_false"
    state = runtime_state()
    if state not in allowed_states():
        return True, f"state_{state.lower()}"
    return False, "allowed"

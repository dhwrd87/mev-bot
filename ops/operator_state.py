from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import fcntl
from bot.core.canonical_chain import canonicalize_chain_target

ALLOWED_STATES = {"PAUSED", "READY", "TRADING", "DEGRADED", "PANIC", "UNKNOWN"}
ALLOWED_MODES = {"dryrun", "paper", "live", "UNKNOWN"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> Dict[str, Any]:
    return {
        "state": "UNKNOWN",
        "mode": "UNKNOWN",
        "kill_switch": False,
        "chain_target": "UNKNOWN",
        "strategy_overrides": {"allowlist": [], "denylist": []},
        "flashloan_enabled": False,
        "limits": {
            "max_fee_gwei": None,
            "slippage_bps": None,
            "max_daily_loss_usd": None,
            "min_edge_bps": None,
        },
        "risk_overrides": {
            "allow_tokens": [],
            "deny_tokens": [],
            "watch_tokens": [],
            "allow_pools": [],
            "deny_pools": [],
            "watch_pools": [],
        },
        "enabled_dex_overrides": {"allowlist": [], "denylist": []},
        "dex_packs_enabled": [],
        "dex_packs_disabled": [],
        "status_message_id": None,
        "last_updated": utc_now_iso(),
        "last_actor": "system",
    }


def _normalize_actor(actor: str | None) -> str:
    val = str(actor or "").strip()
    return val or "system"


def _normalize_slug_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [x.strip().lower() for x in value.split(",")]
        return [x for x in items if x]
    if isinstance(value, (list, tuple, set)):
        out = []
        for x in value:
            s = str(x).strip().lower()
            if s:
                out.append(s)
        return out
    return []


def _as_optional_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        raise ValueError(f"invalid numeric value '{v}'")


def validate_state(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("operator state must be an object")

    state = str(raw.get("state", "UNKNOWN")).strip().upper()
    raw_mode = str(raw.get("mode", "UNKNOWN")).strip()
    mode = "UNKNOWN" if raw_mode.upper() == "UNKNOWN" else raw_mode.lower()
    kill_switch = bool(raw.get("kill_switch", False))
    flashloan_enabled = bool(raw.get("flashloan_enabled", False))
    chain_target = canonicalize_chain_target(str(raw.get("chain_target", "UNKNOWN")).strip() or "UNKNOWN")
    status_message_id_raw = raw.get("status_message_id")
    if status_message_id_raw is None or str(status_message_id_raw).strip() == "":
        status_message_id = None
    else:
        try:
            status_message_id = int(status_message_id_raw)
        except Exception as e:
            raise ValueError("invalid status_message_id") from e
        if status_message_id <= 0:
            raise ValueError("invalid status_message_id")
    last_updated = str(raw.get("last_updated", "")).strip() or utc_now_iso()
    last_actor = _normalize_actor(raw.get("last_actor"))
    enabled_dex_overrides_raw = raw.get("enabled_dex_overrides")
    if not isinstance(enabled_dex_overrides_raw, dict):
        enabled_dex_overrides_raw = {}
    allowlist = _normalize_slug_list(enabled_dex_overrides_raw.get("allowlist"))
    denylist = _normalize_slug_list(enabled_dex_overrides_raw.get("denylist"))
    # Backward compatibility: fold legacy keys if present.
    legacy_enable = _normalize_slug_list(raw.get("dex_packs_enabled"))
    legacy_disable = _normalize_slug_list(raw.get("dex_packs_disabled"))
    if not allowlist and legacy_enable:
        allowlist = legacy_enable
    denylist = sorted(set(denylist + legacy_disable))

    if state not in ALLOWED_STATES:
        raise ValueError(f"invalid state '{state}'")
    if mode not in ALLOWED_MODES:
        raise ValueError(f"invalid mode '{mode}'")

    strategy_overrides_raw = raw.get("strategy_overrides")
    if not isinstance(strategy_overrides_raw, dict):
        strategy_overrides_raw = {}
    strategy_allow = _normalize_slug_list(strategy_overrides_raw.get("allowlist"))
    strategy_deny = _normalize_slug_list(strategy_overrides_raw.get("denylist"))

    limits_raw = raw.get("limits")
    if not isinstance(limits_raw, dict):
        limits_raw = {}
    limits = {
        "max_fee_gwei": _as_optional_float(limits_raw.get("max_fee_gwei")),
        "slippage_bps": _as_optional_float(limits_raw.get("slippage_bps")),
        "max_daily_loss_usd": _as_optional_float(limits_raw.get("max_daily_loss_usd")),
        "min_edge_bps": _as_optional_float(limits_raw.get("min_edge_bps")),
    }

    risk_raw = raw.get("risk_overrides")
    if not isinstance(risk_raw, dict):
        risk_raw = {}
    risk_overrides = {
        "allow_tokens": _normalize_slug_list(risk_raw.get("allow_tokens")),
        "deny_tokens": _normalize_slug_list(risk_raw.get("deny_tokens")),
        "watch_tokens": _normalize_slug_list(risk_raw.get("watch_tokens")),
        "allow_pools": _normalize_slug_list(risk_raw.get("allow_pools")),
        "deny_pools": _normalize_slug_list(risk_raw.get("deny_pools")),
        "watch_pools": _normalize_slug_list(risk_raw.get("watch_pools")),
    }

    return {
        "state": state,
        "mode": mode,
        "kill_switch": kill_switch,
        "flashloan_enabled": flashloan_enabled,
        "chain_target": chain_target,
        "strategy_overrides": {"allowlist": strategy_allow, "denylist": strategy_deny},
        "strategy_enabled": strategy_allow,
        "strategy_disabled": strategy_deny,
        "limits": limits,
        "risk_overrides": risk_overrides,
        "enabled_dex_overrides": {"allowlist": allowlist, "denylist": denylist},
        "dex_packs_enabled": allowlist,
        "dex_packs_disabled": denylist,
        "status_message_id": status_message_id,
        "last_updated": last_updated,
        "last_actor": last_actor,
    }


@contextmanager
def _state_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_state(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return default_state()
    with _state_lock(p):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            return validate_state(payload)
        except Exception:
            return default_state()


def save_state(path: str | Path, state: Dict[str, Any], *, actor: str | None = None) -> Dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = validate_state(state)
    normalized["last_updated"] = utc_now_iso()
    normalized["last_actor"] = _normalize_actor(actor) if actor is not None else _normalize_actor(
        normalized.get("last_actor")
    )

    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    with _state_lock(p):
        tmp.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    return normalized


def update_state(path: str | Path, patch: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    current = load_state(path)
    merged = dict(current)
    merged.update(patch)
    return save_state(path, merged, actor=actor)

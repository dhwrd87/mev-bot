from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

_CACHE_PATH: str | None = None
_CACHE_TS: float = 0.0
_CACHE_MTIME: float | None = None
_CACHE_DATA: Dict[str, Any] | None = None


def _default_state() -> Dict[str, Any]:
    return {
        "state": "TRADING",
        "mode": "paper",
        "kill_switch": False,
        "flashloan_enabled": False,
        "strategy_overrides": {"allowlist": [], "denylist": []},
        "strategy_enabled": [],
        "strategy_disabled": [],
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
        "last_actor": "system",
    }


def get_operator_state(path: str | None = None) -> Dict[str, Any]:
    global _CACHE_PATH, _CACHE_TS, _CACHE_MTIME, _CACHE_DATA

    p = str(path or os.getenv("OPERATOR_STATE_PATH", "/app/ops/operator_state.json")).strip()
    now = time.monotonic()

    if _CACHE_DATA is not None and _CACHE_PATH == p and (now - _CACHE_TS) < 1.0:
        return dict(_CACHE_DATA)

    fp = Path(p)
    try:
        mtime = fp.stat().st_mtime
    except Exception:
        mtime = None

    if _CACHE_DATA is not None and _CACHE_PATH == p and _CACHE_MTIME == mtime:
        _CACHE_TS = now
        return dict(_CACHE_DATA)

    data = _default_state()
    if mtime is not None:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["state"] = str(raw.get("state", data["state"])).upper()
                data["mode"] = str(raw.get("mode", data["mode"]))
                data["kill_switch"] = bool(raw.get("kill_switch", False))
                data["flashloan_enabled"] = bool(raw.get("flashloan_enabled", False))
                data["last_actor"] = str(raw.get("last_actor", data["last_actor"]))
                strategy_overrides = raw.get("strategy_overrides")
                if isinstance(strategy_overrides, dict):
                    allow = strategy_overrides.get("allowlist", [])
                    deny = strategy_overrides.get("denylist", [])
                    data["strategy_overrides"] = {
                        "allowlist": [str(x).strip().lower() for x in allow if str(x).strip()],
                        "denylist": [str(x).strip().lower() for x in deny if str(x).strip()],
                    }
                data["strategy_enabled"] = [
                    str(x).strip().lower() for x in raw.get("strategy_enabled", []) if str(x).strip()
                ]
                data["strategy_disabled"] = [
                    str(x).strip().lower() for x in raw.get("strategy_disabled", []) if str(x).strip()
                ]
                if not data["strategy_overrides"]["allowlist"] and data["strategy_enabled"]:
                    data["strategy_overrides"]["allowlist"] = list(data["strategy_enabled"])
                if data["strategy_disabled"]:
                    merged_sd = list(data["strategy_overrides"]["denylist"]) + list(data["strategy_disabled"])
                    data["strategy_overrides"]["denylist"] = sorted(set(merged_sd))
                limits = raw.get("limits")
                if isinstance(limits, dict):
                    data["limits"] = {
                        "max_fee_gwei": limits.get("max_fee_gwei"),
                        "slippage_bps": limits.get("slippage_bps"),
                        "max_daily_loss_usd": limits.get("max_daily_loss_usd"),
                        "min_edge_bps": limits.get("min_edge_bps"),
                    }
                overrides = raw.get("enabled_dex_overrides")
                if isinstance(overrides, dict):
                    allow = overrides.get("allowlist", [])
                    deny = overrides.get("denylist", [])
                    data["enabled_dex_overrides"] = {
                        "allowlist": [str(x).strip().lower() for x in allow if str(x).strip()],
                        "denylist": [str(x).strip().lower() for x in deny if str(x).strip()],
                    }
                data["dex_packs_enabled"] = [
                    str(x).strip().lower() for x in raw.get("dex_packs_enabled", []) if str(x).strip()
                ]
                data["dex_packs_disabled"] = [
                    str(x).strip().lower() for x in raw.get("dex_packs_disabled", []) if str(x).strip()
                ]
                if not data["enabled_dex_overrides"]["allowlist"] and data["dex_packs_enabled"]:
                    data["enabled_dex_overrides"]["allowlist"] = list(data["dex_packs_enabled"])
                if data["dex_packs_disabled"]:
                    merged = list(data["enabled_dex_overrides"]["denylist"]) + list(data["dex_packs_disabled"])
                    data["enabled_dex_overrides"]["denylist"] = sorted(set(merged))
                risk_raw = raw.get("risk_overrides")
                if isinstance(risk_raw, dict):
                    data["risk_overrides"] = {
                        "allow_tokens": [str(x).strip().lower() for x in risk_raw.get("allow_tokens", []) if str(x).strip()],
                        "deny_tokens": [str(x).strip().lower() for x in risk_raw.get("deny_tokens", []) if str(x).strip()],
                        "watch_tokens": [str(x).strip().lower() for x in risk_raw.get("watch_tokens", []) if str(x).strip()],
                        "allow_pools": [str(x).strip().lower() for x in risk_raw.get("allow_pools", []) if str(x).strip()],
                        "deny_pools": [str(x).strip().lower() for x in risk_raw.get("deny_pools", []) if str(x).strip()],
                        "watch_pools": [str(x).strip().lower() for x in risk_raw.get("watch_pools", []) if str(x).strip()],
                    }
        except Exception:
            pass

    _CACHE_PATH = p
    _CACHE_TS = now
    _CACHE_MTIME = mtime
    _CACHE_DATA = dict(data)
    return data


def operator_block_reason(path: str | None = None) -> tuple[bool, Dict[str, Any], str]:
    st = get_operator_state(path=path)
    if bool(st.get("kill_switch", False)):
        return True, st, "kill_switch"
    if str(st.get("state", "UNKNOWN")).upper() != "TRADING":
        return True, st, "state_not_trading"
    return False, st, "allowed"

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.core.canonical_chain import canonicalize_chain_target


@dataclass(frozen=True)
class ResolvedChain:
    chain_key: str
    chain_id: int
    rpc_http: str
    rpc_ws: str
    explorer_base: str
    family: str
    native_symbol: str
    profile: str
    config_validation_ok: bool
    config_validation_reasons: list[str]


def _read_doc(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except Exception:
        import yaml  # type: ignore

        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"invalid object document: {path}")
    return raw


def _registry_path() -> Path:
    return Path(
        os.getenv(
            "MEVBOT_CHAIN_REGISTRY_PATH",
            str(Path(__file__).resolve().parents[2] / "config" / "chains.yaml"),
        )
    )


def _profiles_base() -> Path:
    return Path(
        os.getenv(
            "CHAIN_PROFILES_DIR",
            str(_registry_path().resolve().parent / "chains"),
        )
    )


def _secrets_path() -> Path:
    return Path(
        os.getenv(
            "MEVBOT_SECRETS_PATH",
            str(Path(__file__).resolve().parents[2] / "runtime" / "secrets.runtime.json"),
        )
    )


def _load_registry_chains() -> dict[str, dict[str, Any]]:
    p = _registry_path()
    if not p.exists():
        raise ValueError(f"chain registry missing: {p}")
    raw = _read_doc(p)
    chains = raw.get("chains", {})
    if not isinstance(chains, dict) or not chains:
        raise ValueError(f"invalid chains registry at {p}: missing chains object")
    out: dict[str, dict[str, Any]] = {}
    for key, value in chains.items():
        if not isinstance(value, dict):
            continue
        out[str(key).strip().lower()] = dict(value)
    if not out:
        raise ValueError(f"invalid chains registry at {p}: no chain entries")
    return out


def _overlay_to_entry(raw: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    chain = str(raw.get("chain", "")).strip().lower()
    if not chain:
        return None
    mapped: dict[str, Any] = {}
    if raw.get("chain_id") is not None:
        mapped["chain_id"] = raw.get("chain_id")
    fam = str(raw.get("family", "") or raw.get("chain_family", "")).strip().lower()
    if fam:
        mapped["family"] = fam
        mapped["chain_family"] = fam.upper()
    if raw.get("default_rpc_http"):
        mapped["default_rpc_http"] = raw.get("default_rpc_http")
    if isinstance(raw.get("default_rpc_http_backups"), list):
        mapped["default_rpc_http_backups"] = raw.get("default_rpc_http_backups")
    if isinstance(raw.get("default_ws_endpoints"), list):
        mapped["default_ws_endpoints"] = raw.get("default_ws_endpoints")
    if raw.get("explorer_base_url"):
        mapped["explorer_base_url"] = raw.get("explorer_base_url")
    if raw.get("native_symbol"):
        mapped["native_symbol"] = raw.get("native_symbol")

    # profile schema overlay support
    rpc = raw.get("rpc")
    if isinstance(rpc, dict) and isinstance(rpc.get("endpoints"), list):
        http_vals: list[str] = []
        ws_vals: list[str] = []
        for ep in rpc["endpoints"]:
            if not isinstance(ep, dict):
                continue
            h = str(ep.get("http", "")).strip()
            w = str(ep.get("ws", "")).strip()
            if h:
                http_vals.append(h)
            if w:
                ws_vals.append(w)
        if http_vals:
            mapped["default_rpc_http"] = http_vals[0]
            if len(http_vals) > 1:
                mapped["default_rpc_http_backups"] = http_vals[1:]
        if ws_vals:
            mapped["default_ws_endpoints"] = ws_vals
    explorer = raw.get("explorer")
    if isinstance(explorer, dict):
        base = str(explorer.get("base_url", "")).strip()
        if base:
            mapped["explorer_base_url"] = base
    native_asset = raw.get("native_asset")
    if isinstance(native_asset, dict):
        sym = str(native_asset.get("symbol", "")).strip()
        if sym:
            mapped["native_symbol"] = sym
    return chain, mapped


def _apply_profile_overlays(chains: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    base = _profiles_base()
    if not base.exists():
        return chains
    out = {k: dict(v) for k, v in chains.items()}
    for fp in sorted(base.rglob("*.yaml")):
        try:
            raw = _read_doc(fp)
        except Exception:
            continue
        mapped = _overlay_to_entry(raw)
        if mapped is None:
            continue
        chain, patch = mapped
        current = dict(out.get(chain, {}))
        current.update({k: v for k, v in patch.items() if v not in (None, "", [])})
        out[chain] = current
    return out


def _load_secrets() -> dict[str, Any]:
    p = _secrets_path()
    if not p.exists():
        return {}
    try:
        raw = _read_doc(p)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for val in values:
        v = str(val).strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _supported_families() -> set[str]:
    raw = str(os.getenv("SUPPORTED_FAMILIES", "EVM")).strip()
    out: set[str] = set()
    for part in raw.split(","):
        p = str(part).strip().lower()
        if p in {"evm", "ethereum"}:
            out.add("evm")
        elif p in {"sol", "solana"}:
            out.add("sol")
    return out or {"evm"}


def _expand_placeholders(value: str, secrets: dict[str, Any]) -> str:
    out = str(value or "")
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for key in pattern.findall(out):
        secret_keys = secrets.get("keys", {})
        replacement = ""
        if isinstance(secret_keys, dict):
            replacement = str(secret_keys.get(key, "")).strip()
        if not replacement:
            replacement = str(os.getenv(key, "")).strip()
        out = out.replace(f"${{{key}}}", replacement)
    return out


def _resolve_secret_ref(value: str, secrets: dict[str, Any]) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("secret://"):
        # secret://rpc_http/sepolia or secret://aliases/publicnode_sepolia_http
        _, _, tail = s.partition("secret://")
        section, _, name = tail.partition("/")
        bucket = secrets.get(section, {})
        if isinstance(bucket, dict):
            return str(bucket.get(name, "")).strip()
        return ""
    if s.startswith("ref:"):
        # ref:rpc_http.sepolia
        _, _, tail = s.partition("ref:")
        section, _, name = tail.partition(".")
        bucket = secrets.get(section, {})
        if isinstance(bucket, dict):
            return str(bucket.get(name, "")).strip()
        return ""
    if s.startswith("alias://"):
        _, _, name = s.partition("alias://")
        aliases = secrets.get("aliases", {})
        if isinstance(aliases, dict):
            return str(aliases.get(name, "")).strip()
        return ""
    return s


def _resolve_url(value: str, secrets: dict[str, Any]) -> str:
    ref = _resolve_secret_ref(value, secrets)
    return _expand_placeholders(ref, secrets).strip()


def _pick_chain_key(chain_key: str | None) -> str:
    raw = str(
        chain_key
        or os.getenv("CHAIN_KEY")
        or os.getenv("CHAIN")
        or os.getenv("MEVBOT_CHAIN_DEFAULT")
        or ""
    ).strip()
    if not raw:
        raise ValueError("CHAIN/CHAIN_KEY is required")
    target = canonicalize_chain_target(raw)
    if target == "UNKNOWN":
        raise ValueError(f"unsupported chain key: {raw}")
    return target


def _split_target(target: str) -> tuple[str, str]:
    fam, chain = target.split(":", 1)
    return fam.lower(), chain.lower()


def _validate(
    *,
    chain_key: str,
    family: str,
    chain: str,
    chain_id: int,
    rpc_http: str,
) -> list[str]:
    reasons: list[str] = []
    if family not in {"evm", "sol"}:
        reasons.append(f"unknown_family:{family}")
    expected = canonicalize_chain_target(f"{family}:{chain}")
    if expected != chain_key:
        reasons.append(f"chain_key_mismatch:{chain_key}!={expected}")
    if not str(rpc_http).strip():
        reasons.append("rpc_http_missing")
    # EVM chains must have non-zero chain id.
    if family == "evm" and int(chain_id) <= 0:
        reasons.append("chain_id_invalid")
    return reasons


def resolve_chain(chain_key: str | None = None) -> ResolvedChain:
    target = _pick_chain_key(chain_key)
    family, chain = _split_target(target)
    registry = _apply_profile_overlays(_load_registry_chains())
    entry = dict(registry.get(chain, {}))

    # Backward compatibility alias.
    if chain == "ethereum" and "ethereum" not in registry and "mainnet" in registry:
        chain = "mainnet"
        entry = dict(registry.get(chain, {}))

    if not entry:
        raise ValueError(f"config_error: chain_not_found:{chain}")

    secrets = _load_secrets()
    entry_family = (
        str(entry.get("family", "")).strip().lower()
        or str(entry.get("chain_family", "")).strip().lower()
        or family
    )
    if entry_family not in _supported_families():
        supported = ",".join(sorted(f.upper() for f in _supported_families()))
        raise ValueError(f"config_error: unsupported_family:{entry_family}:SUPPORTED_FAMILIES={supported}")

    # resolve RPC HTTP candidates
    http_candidates: list[str] = []
    extra_http = [x.strip() for x in str(os.getenv("RPC_HTTP_EXTRA", "")).split(",") if x.strip()]
    http_candidates.extend(extra_http)
    http_candidates.append(str(entry.get("default_rpc_http", "")).strip())
    http_candidates.extend([str(x).strip() for x in entry.get("default_rpc_http_backups", []) if str(x).strip()])
    resolved_http = _dedupe([_resolve_url(v, secrets) for v in http_candidates])

    # resolve WS candidates
    ws_candidates: list[str] = []
    extra_ws = [x.strip() for x in str(os.getenv("WS_ENDPOINTS_EXTRA", "")).split(",") if x.strip()]
    ws_candidates.extend(extra_ws)
    ws_candidates.extend([str(x).strip() for x in entry.get("default_ws_endpoints", []) if str(x).strip()])
    resolved_ws = _dedupe([_resolve_url(v, secrets) for v in ws_candidates])

    chain_id = int(entry.get("chain_id", 0) or 0)
    explorer_base = str(entry.get("explorer_base_url", "")).strip()
    reasons = _validate(
        chain_key=target,
        family=entry_family,
        chain=chain,
        chain_id=chain_id,
        rpc_http=resolved_http[0] if resolved_http else "",
    )
    if reasons:
        raise ValueError("config_error: " + ",".join(reasons))

    return ResolvedChain(
        chain_key=target,
        chain_id=chain_id,
        rpc_http=resolved_http[0] if resolved_http else "",
        rpc_ws=resolved_ws[0] if resolved_ws else "",
        explorer_base=explorer_base,
        family=entry_family,
        native_symbol=str(entry.get("native_symbol", "")).strip(),
        profile=f"registry:{_registry_path()}",
        config_validation_ok=True,
        config_validation_reasons=[],
    )

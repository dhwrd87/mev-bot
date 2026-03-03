from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List


@dataclass(frozen=True)
class ChainConfig:
    chain: str
    chain_id: int
    explorer_base_url: str
    native_symbol: str
    ws_endpoints_selected: List[str]
    rpc_http_selected: str
    rpc_http_backups: List[str]

    # Backward-compatible aliases
    @property
    def ws_endpoints(self) -> List[str]:
        return self.ws_endpoints_selected

    @property
    def rpc_http(self) -> str:
        return self.rpc_http_selected


_CACHE: ChainConfig | None = None


def _csv(v: str | None) -> List[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def _dedupe(seq: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in seq:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _expand_placeholders(url: str) -> str | None:
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    matches = pattern.findall(url)
    out = url
    for key in matches:
        val = (os.getenv(key) or "").strip()
        if not val:
            return None
        out = out.replace(f"${{{key}}}", val)
    return out


def _normalize_urls(values: List[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        expanded = _expand_placeholders(str(v).strip())
        if expanded:
            out.append(expanded)
    return out


def _chains_file() -> Path:
    env_path = os.getenv("CHAINS_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[2] / "config" / "chains.yaml"


def _profile_dir() -> Path:
    env_path = os.getenv("CHAIN_PROFILES_DIR")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[2] / "config" / "chains"


def _read_jsonish(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"profile must be object: {path}")
    return raw


def _load_profile_chains() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    pdir = _profile_dir()
    if not pdir.exists():
        return out
    for p in sorted(pdir.rglob("*.yaml")):
        try:
            raw = _read_jsonish(p)
        except Exception:
            continue
        chain = str(raw.get("chain", "")).strip().lower()
        if not chain:
            continue
        # Normalize profile schema into chain registry schema.
        mapped = {
            "chain_id": raw.get("chain_id", 0),
            "default_rpc_http": raw.get("default_rpc_http", ""),
            "default_rpc_http_backups": raw.get("default_rpc_http_backups", []),
            "default_ws_endpoints": raw.get("default_ws_endpoints", []),
            "explorer_base_url": raw.get("explorer_base_url", ""),
            "native_symbol": raw.get("native_symbol", ""),
        }
        # Only override keys if profile actually provided them.
        if chain in out:
            for k, v in mapped.items():
                if v not in ("", [], 0, None):
                    out[chain][k] = v
        else:
            out[chain] = mapped
    return out


def _load_chains() -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    p = _chains_file()
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"chains config must be valid JSON-in-YAML at {p}: {e}") from e
        chains = raw.get("chains", {})
        if isinstance(chains, dict):
            merged.update({str(k).lower(): v for k, v in chains.items() if isinstance(v, dict)})
    for chain, prof in _load_profile_chains().items():
        base = merged.get(chain, {})
        if not isinstance(base, dict):
            base = {}
        updated = dict(base)
        for k, v in prof.items():
            if v not in ("", [], 0, None):
                updated[k] = v
        merged[chain] = updated
    if not merged:
        raise ValueError(f"invalid chains config: no chains found in {p} or {_profile_dir()}")
    return merged


def get_chain_config() -> ChainConfig:
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    chain = (os.getenv("CHAIN") or "").strip().lower()
    if not chain:
        raise ValueError("CHAIN is required")
    if chain == "ethereum":
        chain = "mainnet"

    chains = _load_chains()
    if chain not in chains:
        raise ValueError(f"Unsupported CHAIN '{chain}'. Supported: {', '.join(sorted(chains.keys()))}")
    c = chains[chain]

    ws_defaults = _normalize_urls([str(x).strip() for x in c.get("default_ws_endpoints", []) if str(x).strip()])
    rpc_defaults = _normalize_urls(
        [str(c.get("default_rpc_http", "")).strip()]
        + [str(x).strip() for x in c.get("default_rpc_http_backups", []) if str(x).strip()]
    )

    ws_extra = _normalize_urls(_csv(os.getenv("WS_ENDPOINTS_EXTRA")))
    rpc_extra = _normalize_urls(_csv(os.getenv("RPC_HTTP_EXTRA")))

    ws = _dedupe(ws_extra + ws_defaults)
    rpc = _dedupe(rpc_extra + rpc_defaults)
    if not ws:
        raise ValueError(f"No WS endpoints configured for CHAIN={chain}")
    if not rpc:
        raise ValueError(f"No HTTP RPC endpoints configured for CHAIN={chain}")

    chain_id = int(os.getenv("CHAIN_ID") or c.get("chain_id"))
    cfg = ChainConfig(
        chain=chain,
        chain_id=chain_id,
        explorer_base_url=str(c.get("explorer_base_url", "")),
        native_symbol=str(c.get("native_symbol", "")),
        ws_endpoints_selected=ws,
        rpc_http_selected=rpc[0],
        rpc_http_backups=rpc[1:],
    )
    _CACHE = cfg
    return cfg


def _reset_chain_config_cache_for_tests() -> None:
    global _CACHE
    _CACHE = None

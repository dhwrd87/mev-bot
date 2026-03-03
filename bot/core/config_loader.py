from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RPCEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    http: str = ""
    ws: str = ""

    @model_validator(mode="after")
    def _check_transport(self) -> "RPCEndpoint":
        if not str(self.http).strip() and not str(self.ws).strip():
            raise ValueError("rpc endpoint requires at least one of http/ws")
        return self


class RPCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoints: list[RPCEndpoint] = Field(min_length=1)


class ExplorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    tx_url: str
    address_url: str
    block_url: str


class NativeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    decimals: int = Field(ge=0)


class RiskDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_fee_gwei: float = Field(ge=0)
    slippage_bps: int = Field(ge=0)
    max_daily_loss_usd: float = Field(ge=0)
    min_edge_bps: float = Field(ge=0)


class EVMUniV2Config(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["evm_univ2"]
    router: str
    factory: str
    fee_bps: int = Field(ge=0, le=10_000)


class EVMUniV3Config(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["evm_univ3"]
    factory: str
    quoter: str
    swap_router: str
    fee_tiers: list[int] = Field(min_length=1)


class SolJupiterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["sol_jupiter"]
    base_url: str
    timeout_ms: int = Field(gt=0)


DexConfig = EVMUniV2Config | EVMUniV3Config | SolJupiterConfig


class ChainProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    family: Literal["evm", "sol"]
    chain: str
    network: Literal["mainnet", "testnet", "devnet"]
    chain_id: int | None = None
    rpc: RPCConfig
    explorer: ExplorerConfig
    native_asset: NativeAsset
    risk: RiskDefaults
    dexes_enabled: list[str] = Field(min_length=1)
    dex_configs: dict[str, DexConfig]

    @field_validator("chain")
    @classmethod
    def _chain_nonempty(cls, v: str) -> str:
        out = str(v).strip().lower()
        if not out:
            raise ValueError("chain is required")
        return out

    @model_validator(mode="after")
    def _check_dex_keys(self) -> "ChainProfile":
        missing = [d for d in self.dexes_enabled if d not in self.dex_configs]
        if missing:
            raise ValueError(f"dexes_enabled missing dex_configs entries: {missing}")
        return self


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_yaml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        out = yaml.safe_load(text)
    except Exception:
        out = json.loads(text)
    if not isinstance(out, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return out


def _chain_profiles_dir() -> Path:
    return Path(os.getenv("CHAIN_PROFILES_DIR", str(_project_root() / "config" / "chains")))


def _dex_defaults_dir() -> Path:
    return Path(os.getenv("DEX_PACK_DEFAULTS_DIR", str(_project_root() / "config" / "dex_packs")))


def _expand_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_placeholders(v) for v in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1].strip()
        env_val = os.getenv(env_key)
        return env_val if env_val is not None else value
    return value


def _find_chain_profile(profile_name: str, base_dir: Path | None = None) -> Path:
    base = base_dir or _chain_profiles_dir()
    name = str(profile_name or "").strip()
    if not name:
        raise ValueError("CHAIN_PROFILE is required")
    if name.endswith(".yaml"):
        candidate = base / name
        if candidate.exists():
            return candidate
    if "/" in name:
        candidate = base / f"{name}.yaml"
        if candidate.exists():
            return candidate
    matches = sorted(base.rglob(f"{name}.yaml"))
    if not matches:
        raise FileNotFoundError(f"chain profile not found: {name} under {base}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous chain profile '{name}': {[str(m) for m in matches]}")
    return matches[0]


def _load_dex_default(dex_type: str, defaults_dir: Path | None = None) -> Dict[str, Any]:
    d = defaults_dir or _dex_defaults_dir()
    p = d / f"{dex_type}.yaml"
    if not p.exists():
        return {}
    raw = _parse_yaml(p)
    return _expand_placeholders(raw)


def load_chain_profile(profile_name: str | None = None) -> ChainProfile:
    profile = str(profile_name or os.getenv("CHAIN_PROFILE", "")).strip()
    profile_path = _find_chain_profile(profile)
    raw = _expand_placeholders(_parse_yaml(profile_path))

    dex_cfg_raw = raw.get("dex_configs")
    if not isinstance(dex_cfg_raw, dict):
        raise ValueError(f"dex_configs must be an object in {profile_path}")

    merged: Dict[str, Any] = {}
    for name, cfg in dex_cfg_raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"dex_configs.{name} must be object in {profile_path}")
        dex_type = str(cfg.get("type", "")).strip()
        if not dex_type:
            raise ValueError(f"dex_configs.{name}.type missing in {profile_path}")
        defaults = _load_dex_default(dex_type)
        merged_cfg = {**defaults, **cfg, "type": dex_type}
        merged[name] = merged_cfg

    payload = dict(raw)
    payload["dex_configs"] = merged
    model = ChainProfile.model_validate(payload)
    return model


def load_chain_profile_dict(profile_name: str | None = None) -> Dict[str, Any]:
    return load_chain_profile(profile_name).model_dump(mode="json")

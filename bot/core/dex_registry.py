from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from adapters.dex_packs.base import DEXPack, UniV2Pack
from adapters.dex_packs.evm_univ2 import EVMUniV2Pack
from adapters.dex_packs.evm_univ3 import EVMUniV3Pack
from adapters.dex_packs.sol_jupiter import SolJupiterPack
from bot.core.canonical import canonicalize_context
from bot.core.config_loader import ChainProfile, load_chain_profile

PackCtor = type[DEXPack]


PACK_CONSTRUCTORS: dict[str, PackCtor] = {
    "univ2": UniV2Pack,
    "evm_univ2": EVMUniV2Pack,
    "univ3": EVMUniV3Pack,
    "evm_univ3": EVMUniV3Pack,
    "jupiter": SolJupiterPack,
    "sol_jupiter": SolJupiterPack,
}


def _csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip().lower() for x in str(raw).split(",") if x.strip()]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _csv(value)
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip().lower() for x in value if str(x).strip()]
    return []


def _chain_profiles_dir() -> Path:
    return Path(os.getenv("CHAIN_PROFILES_DIR", "config/chains"))


def _resolve_profile_name(*, family: str, chain: str, network: str) -> str:
    base = _chain_profiles_dir()
    direct = base / family / f"{chain}-{network}.yaml"
    if direct.exists():
        return f"{family}/{chain}-{network}"

    # fallback: exact chain file under family
    exact = base / family / f"{chain}.yaml"
    if exact.exists():
        return f"{family}/{chain}"

    # fallback: search family subtree for closest match
    matches = sorted(base.glob(f"{family}/**/*{chain}*{network}*.yaml"))
    if matches:
        rel = matches[0].relative_to(base)
        return str(rel.with_suffix(""))

    raise FileNotFoundError(
        f"chain profile not found for family={family} chain={chain} network={network} in {base}"
    )


class DEXPackRegistry:
    def __init__(self, *, operator_state_path: Optional[str] = None) -> None:
        self.operator_state_path = operator_state_path or os.getenv("OPERATOR_STATE_FILE", "ops/operator_state.json")
        self._packs: dict[str, DEXPack] = {}
        self._enabled: set[str] = set()
        self._last_ctx: dict[str, str] = {}
        self._last_profile: str = ""

    @staticmethod
    def _load_profile(*, family: str, chain: str, network: str, profile_name: Optional[str] = None) -> ChainProfile:
        profile = str(profile_name or "").strip() or _resolve_profile_name(
            family=family, chain=chain, network=network
        )
        return load_chain_profile(profile)

    def reload(
        self,
        *,
        family: str,
        chain: str,
        network: Optional[str] = None,
        profile_name: Optional[str] = None,
    ) -> None:
        ctx = canonicalize_context(family=family, chain=chain, network=network)
        fam, ch, net = ctx["family"], ctx["chain"], ctx["network"]

        profile = self._load_profile(family=fam, chain=ch, network=net, profile_name=profile_name)
        self._last_profile = str(profile_name or _resolve_profile_name(family=fam, chain=ch, network=net))
        self._last_ctx = {"family": profile.family, "chain": profile.chain, "network": profile.network}

        enabled = set(_as_list(profile.dexes_enabled))

        env_enable = set(_csv(os.getenv("DEX_PACKS_ENABLE")))
        env_disable = set(_csv(os.getenv("DEX_PACKS_DISABLE")))
        enabled |= env_enable
        enabled -= env_disable

        op = self._read_operator_toggles()
        overrides = op.get("enabled_dex_overrides") if isinstance(op.get("enabled_dex_overrides"), dict) else {}
        op_enable = set(_as_list(overrides.get("allowlist"))) or set(_as_list(op.get("dex_packs_enabled")))
        op_disable = set(_as_list(overrides.get("denylist"))) | set(_as_list(op.get("dex_packs_disabled")))
        if op_enable:
            enabled = op_enable
        enabled -= op_disable

        built: dict[str, DEXPack] = {}
        pack_cfg_map = dict(profile.dex_configs)
        rpc_primary = ""
        try:
            rpc_primary = str(profile.rpc.endpoints[0].http or "").strip()
        except Exception:
            rpc_primary = ""

        for name in sorted(enabled):
            p_cfg = pack_cfg_map.get(name)
            if p_cfg is None:
                continue
            cfg = p_cfg.model_dump(mode="json") if hasattr(p_cfg, "model_dump") else dict(p_cfg)
            ctor_key = str(cfg.get("type", name)).strip().lower()
            ctor = PACK_CONSTRUCTORS.get(ctor_key)
            if not ctor:
                continue
            merged_cfg = dict(cfg)
            merged_cfg.setdefault("rpc_http", rpc_primary or os.getenv("RPC_HTTP_PRIMARY", ""))
            pack = ctor(config=merged_cfg, instance_name=name)
            if pack.supports_context(family=profile.family, chain=profile.chain):
                built[name] = pack

        self._packs = built
        self._enabled = set(built.keys())

    def validate_enabled_pack_configs(
        self,
        *,
        family: str,
        chain: str,
        network: Optional[str] = None,
        profile_name: Optional[str] = None,
    ) -> dict[str, Any]:
        ctx = canonicalize_context(family=family, chain=chain, network=network)
        profile = self._load_profile(
            family=ctx["family"],
            chain=ctx["chain"],
            network=ctx["network"],
            profile_name=profile_name,
        )
        enabled = set(_as_list(profile.dexes_enabled))
        if not enabled:
            return {"ok": True, "validated": 0, "errors": []}

        required_by_type: dict[str, tuple[str, ...]] = {
            "evm_univ2": ("factory", "router"),
            "univ2": ("factory", "router"),
            "evm_univ3": ("factory", "quoter", "swap_router"),
            "univ3": ("factory", "quoter", "swap_router"),
            "jupiter": ("base_url",),
            "sol_jupiter": ("base_url",),
        }

        errors: list[str] = []
        for name in sorted(enabled):
            pcfg = profile.dex_configs.get(name)
            if pcfg is None:
                errors.append(f"{name}:missing_config")
                continue
            payload = pcfg.model_dump(mode="json") if hasattr(pcfg, "model_dump") else dict(pcfg)
            ptype = str(payload.get("type", name)).strip().lower()
            required = required_by_type.get(ptype, ())
            for field in required:
                if not str(payload.get(field, "")).strip():
                    errors.append(f"{name}:missing_{field}")

        return {"ok": not errors, "validated": len(enabled), "errors": errors}

    def enabled_names(self) -> list[str]:
        return sorted(self._enabled)

    def get(self, name: str) -> Optional[DEXPack]:
        return self._packs.get(str(name).strip().lower())

    def list(self) -> list[DEXPack]:
        return [self._packs[k] for k in sorted(self._packs.keys())]

    def choose(self, dex_preference: Optional[str] = None) -> Optional[DEXPack]:
        if dex_preference:
            pack = self.get(dex_preference)
            if pack:
                return pack
        packs = self.list()
        return packs[0] if packs else None

    def apply_runtime_toggle(self, *, enabled: Iterable[str] = (), disabled: Iterable[str] = ()) -> None:
        en = {str(x).strip().lower() for x in enabled if str(x).strip()}
        dis = {str(x).strip().lower() for x in disabled if str(x).strip()}
        names = (self._enabled | en) - dis
        self._enabled = names
        self._packs = {k: v for k, v in self._packs.items() if k in names}

    def _read_operator_toggles(self) -> Dict[str, Any]:
        p = Path(self.operator_state_path)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}


__all__ = ["DEXPackRegistry", "PACK_CONSTRUCTORS"]

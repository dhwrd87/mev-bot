from __future__ import annotations

from pathlib import Path

from bot.core.config_loader import load_chain_profile
from bot.core.dex_registry import DEXPackRegistry


def _profile_names() -> list[str]:
    root = Path("config/chains")
    out: list[str] = []
    for fam in ("evm", "sol"):
        base = root / fam
        if not base.exists():
            continue
        for p in sorted(base.glob("*.yaml")):
            out.append(str(p.relative_to(root).with_suffix("")))
    return out


def test_load_every_chain_profile_and_instantiate_registry():
    profiles = _profile_names()
    assert profiles, "no chain profiles found under config/chains/{evm,sol}"

    reg = DEXPackRegistry(operator_state_path="/tmp/nonexistent_operator_state.json")

    for profile_name in profiles:
        profile = load_chain_profile(profile_name)
        assert profile.family in {"evm", "sol"}
        assert profile.chain
        assert profile.network in {"mainnet", "testnet", "devnet"}

        reg.reload(
            family=profile.family,
            chain=profile.chain,
            network=profile.network,
            profile_name=profile_name,
        )

        enabled = set(profile.dexes_enabled)
        instantiated = set(reg.enabled_names())
        # Every instantiated pack must be declared enabled.
        assert instantiated.issubset(enabled)
        # At least one declared pack should instantiate for each profile.
        assert instantiated, f"no dex packs instantiated for profile {profile_name}"

        validation = reg.validate_enabled_pack_configs(
            family=profile.family,
            chain=profile.chain,
            network=profile.network,
            profile_name=profile_name,
        )
        assert validation.get("validated", 0) >= 1

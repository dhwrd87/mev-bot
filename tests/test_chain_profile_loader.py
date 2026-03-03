from __future__ import annotations

from pathlib import Path

from bot.core.config_loader import _parse_yaml, load_chain_profile


def _profile_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "config" / "chains"
    files = sorted((root / "evm").glob("*.yaml")) + sorted((root / "sol").glob("*.yaml"))
    assert files, "expected chain profile templates under config/chains/{evm,sol}"
    return files


def _dex_default_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "config" / "dex_packs"
    files = sorted(root.glob("*.yaml"))
    assert files, "expected dex defaults under config/dex_packs"
    return files


def test_parse_every_chain_profile_yaml_file():
    for path in _profile_files():
        raw = _parse_yaml(path)
        assert isinstance(raw, dict)
        assert raw.get("family")
        assert raw.get("chain")
        assert raw.get("network")

        enabled = raw.get("dexes_enabled")
        dex_configs = raw.get("dex_configs")
        assert isinstance(enabled, list) and enabled
        assert isinstance(dex_configs, dict)
        for dex_name in enabled:
            assert dex_name in dex_configs, f"{path}: dexes_enabled entry '{dex_name}' missing in dex_configs"
        risk = raw.get("risk")
        assert isinstance(risk, dict), f"{path}: risk defaults are required"
        for key in ("max_fee_gwei", "slippage_bps", "max_daily_loss_usd", "min_edge_bps"):
            assert key in risk, f"{path}: risk.{key} is required"


def test_parse_every_dex_default_yaml_file():
    for path in _dex_default_files():
        raw = _parse_yaml(path)
        assert isinstance(raw, dict)
        assert raw.get("type"), f"{path}: type is required"


def test_load_every_chain_profile_with_defaults():
    for path in _profile_files():
        profile_name = path.stem
        model = load_chain_profile(profile_name)
        assert model.family in {"evm", "sol"}
        assert model.chain
        assert model.network in {"mainnet", "testnet", "devnet"}
        assert model.rpc.endpoints
        assert model.risk.max_fee_gwei >= 0
        assert model.risk.slippage_bps >= 0
        assert model.risk.max_daily_loss_usd >= 0
        assert model.risk.min_edge_bps >= 0
        for dex_name in model.dexes_enabled:
            assert dex_name in model.dex_configs

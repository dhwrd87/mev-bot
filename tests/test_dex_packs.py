import json

from adapters.dex_packs.base import JupiterPack, UniV2Pack
from adapters.dex_packs.evm_univ3 import EVMUniV3Pack
from adapters.dex_packs.registry import DEXPackRegistry
from adapters.dex_packs.sol_jupiter import SolJupiterPack
from bot.core.types_dex import TradeIntent


def _write_cfg(path, enabled, dex_packs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled_dex_packs": enabled, "dex_packs": dex_packs}), encoding="utf-8")


def test_registry_loads_chain_packs_and_env_overrides(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    _write_cfg(
        cfg_dir / "sepolia.yaml",
        ["univ2", "univ3"],
        {"univ2": {"router": "0x1"}, "univ3": {"quoter": "0x2"}},
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("DEX_PACKS_DISABLE", "univ2")
    state = tmp_path / "operator_state.json"
    reg = DEXPackRegistry(operator_state_path=str(state))
    reg.reload(family="evm", chain="sepolia", network="testnet")
    assert reg.enabled_names() == ["univ3"]
    assert isinstance(reg.get("univ3"), EVMUniV3Pack)


def test_registry_respects_operator_state_allowlist(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    _write_cfg(
        cfg_dir / "base.yaml",
        ["univ2", "univ3"],
        {"univ2": {"router": "0x1"}, "univ3": {"quoter": "0x2"}},
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    state = tmp_path / "operator_state.json"
    state.write_text(
        json.dumps({"state": "PAUSED", "dex_packs_enabled": ["univ2"], "dex_packs_disabled": ["univ3"]}),
        encoding="utf-8",
    )
    reg = DEXPackRegistry(operator_state_path=str(state))
    reg.reload(family="evm", chain="base", network="mainnet")
    assert reg.enabled_names() == ["univ2"]
    assert isinstance(reg.get("univ2"), UniV2Pack)


def test_registry_respects_enabled_dex_overrides_dict(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    _write_cfg(
        cfg_dir / "base.yaml",
        ["univ2", "univ3"],
        {"univ2": {"router": "0x1"}, "univ3": {"quoter": "0x2"}},
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    state = tmp_path / "operator_state.json"
    state.write_text(
        json.dumps(
            {
                "state": "PAUSED",
                "enabled_dex_overrides": {"allowlist": ["univ3"], "denylist": ["univ2"]},
            }
        ),
        encoding="utf-8",
    )
    reg = DEXPackRegistry(operator_state_path=str(state))
    reg.reload(family="evm", chain="base", network="mainnet")
    assert reg.enabled_names() == ["univ3"]
    assert isinstance(reg.get("univ3"), EVMUniV3Pack)


def test_stub_pack_quote_build_simulate():
    intent = TradeIntent(
        family="sol",
        chain="solana",
        network="devnet",
        dex_preference="jupiter",
        token_in="So11111111111111111111111111111111111111112",
        token_out="USDC111111111111111111111111111111111111111",
        amount_in=1_000_000,
        slippage_bps=50,
        ttl_s=30,
        strategy="default",
    )
    pack = JupiterPack(config={"base_url": "https://quote-api.jup.ag/v6"})
    q = pack.quote(intent)
    assert q.dex == "jupiter"
    assert q.expected_out > 0
    plan = pack.build(intent, q)
    assert plan.instruction_bundle is not None
    sim = pack.simulate(plan)
    assert sim.ok is True


def test_registry_expands_pack_env_placeholders(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    _write_cfg(
        cfg_dir / "solana.yaml",
        ["jupiter"],
        {"jupiter": {"base_url": "https://quote-api.jup.ag/v6", "api_key": "${JUPITER_API_KEY}"}},
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("JUPITER_API_KEY", "k1")
    reg = DEXPackRegistry(operator_state_path=str(tmp_path / "operator_state.json"))
    reg.reload(family="sol", chain="solana", network="devnet")
    pack = reg.get("jupiter")
    assert isinstance(pack, SolJupiterPack)
    assert pack.config.get("api_key") == "k1"


def test_registry_loads_profile_path_and_validates_required_fields(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    prof = cfg_dir / "evm" / "base-mainnet.yaml"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(
        json.dumps(
            {
                "chain": "base",
                "family": "evm",
                "network": "mainnet",
                "enabled_dex_packs": ["u2", "u3"],
                "dex_packs": {
                    "u2": {"type": "evm_univ2", "factory": "0x1", "router": "0x2"},
                    "u3": {"type": "univ3", "factory": "0x3", "quoter": "0x4", "swap_router": "0x5"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    reg = DEXPackRegistry(operator_state_path=str(tmp_path / "operator_state.json"))
    reg.reload(family="evm", chain="base", network="mainnet")
    assert reg.enabled_names() == ["u2", "u3"]
    out = reg.validate_enabled_pack_configs(family="evm", chain="base", network="mainnet")
    assert out["ok"] is True
    assert out["validated"] == 2


def test_registry_validation_reports_missing_required_fields(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "chains"
    _write_cfg(
        cfg_dir / "sepolia.yaml",
        ["u2", "u3", "jup"],
        {
            "u2": {"type": "evm_univ2", "factory": "0x1"},
            "u3": {"type": "evm_univ3", "factory": "0x2", "quoter": "0x3"},
            "jup": {"type": "jupiter"},
        },
    )
    monkeypatch.setenv("DEX_PACK_CONFIG_DIR", str(cfg_dir))
    reg = DEXPackRegistry(operator_state_path=str(tmp_path / "operator_state.json"))
    out = reg.validate_enabled_pack_configs(family="evm", chain="sepolia", network="testnet")
    assert out["ok"] is False
    assert any("u2:missing_router" in e for e in out["errors"])
    assert any("u3:missing_swap_router" in e for e in out["errors"])
    assert any("jup:missing_base_url" in e for e in out["errors"])

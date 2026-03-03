import os
import json
import pytest

from bot.core.chain_config import get_chain_config, _reset_chain_config_cache_for_tests


def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("WS_") or key.startswith("RPC_") or key in (
            "CHAIN",
            "CHAIN_ID",
            "INFURA_KEY",
            "WS_ENDPOINTS_EXTRA",
            "RPC_HTTP_EXTRA",
            "CHAINS_CONFIG_PATH",
            "CHAIN_PROFILES_DIR",
        ):
            monkeypatch.delenv(key, raising=False)
    _reset_chain_config_cache_for_tests()


def test_sepolia_defaults(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "sepolia")
    cfg = get_chain_config()
    assert cfg.chain == "sepolia"
    assert cfg.chain_id == 11155111
    assert cfg.ws_endpoints[0] == "wss://ethereum-sepolia-rpc.publicnode.com"
    assert cfg.ws_endpoints[1] == "wss://sepolia.gateway.tenderly.co"
    assert "infura" not in ",".join(cfg.ws_endpoints_selected)
    assert cfg.rpc_http == "https://ethereum-sepolia-rpc.publicnode.com"
    assert cfg.explorer_base_url == "https://sepolia.etherscan.io"
    assert cfg.native_symbol == "ETH"


def test_infura_placeholder_expands_only_when_key_present(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "sepolia")
    monkeypatch.setenv("INFURA_KEY", "abc123")
    cfg = get_chain_config()
    assert cfg.ws_endpoints_selected[-1].startswith("wss://sepolia.infura.io/ws/v3/abc123")
    assert cfg.rpc_http_backups[-1].startswith("https://sepolia.infura.io/v3/abc123")


def test_extra_endpoints_prepend_and_dedupe(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "sepolia")
    monkeypatch.setenv("WS_ENDPOINTS_EXTRA", "wss://example.ws,wss://ethereum-sepolia-rpc.publicnode.com,wss://example.ws")
    monkeypatch.setenv("RPC_HTTP_EXTRA", "https://example.rpc,https://ethereum-sepolia-rpc.publicnode.com")
    cfg = get_chain_config()
    assert cfg.ws_endpoints_selected[0] == "wss://example.ws"
    assert cfg.ws_endpoints_selected.count("wss://example.ws") == 1
    assert cfg.rpc_http == "https://example.rpc"


def test_amoy_defaults(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "amoy")
    cfg = get_chain_config()
    assert cfg.chain_id == 80002
    assert cfg.ws_endpoints[0] == "wss://polygon-amoy-bor-rpc.publicnode.com"
    assert cfg.rpc_http == "https://polygon-amoy-bor-rpc.publicnode.com"


def test_chain_id_env_override(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "sepolia")
    monkeypatch.setenv("CHAIN_ID", "12345")
    cfg = get_chain_config()
    assert cfg.chain_id == 12345


def test_unsupported_chain(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("CHAIN", "unknown")
    with pytest.raises(ValueError):
        get_chain_config()


def test_profile_directory_overrides_registry(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    chains_path = tmp_path / "chains.yaml"
    chains_path.write_text(json.dumps({"chains": {}}), encoding="utf-8")
    prof_dir = tmp_path / "profiles" / "evm"
    prof_dir.mkdir(parents=True, exist_ok=True)
    (prof_dir / "base-mainnet.yaml").write_text(
        json.dumps(
            {
                "chain": "base",
                "family": "evm",
                "network": "mainnet",
                "chain_id": 8453,
                "default_rpc_http": "https://example-base-rpc",
                "default_ws_endpoints": ["wss://example-base-ws"],
                "explorer_base_url": "https://basescan.org",
                "native_symbol": "ETH",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHAINS_CONFIG_PATH", str(chains_path))
    monkeypatch.setenv("CHAIN_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("CHAIN", "base")
    cfg = get_chain_config()
    assert cfg.rpc_http == "https://example-base-rpc"
    assert cfg.ws_endpoints_selected == ["wss://example-base-ws"]

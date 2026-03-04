from pathlib import Path

from bot.core.chain_registry import resolve_chain


def test_resolve_chain_from_registry(monkeypatch):
    monkeypatch.setenv("MEVBOT_CHAIN_REGISTRY_PATH", "config/chains.yaml")
    monkeypatch.setenv("CHAIN_PROFILES_DIR", "config/chains")
    monkeypatch.setenv("CHAIN_KEY", "EVM:sepolia")
    rc = resolve_chain()
    assert rc.chain_key == "EVM:sepolia"
    assert rc.chain_id == 11155111
    assert rc.rpc_http
    assert rc.family == "evm"
    assert rc.config_validation_ok is True


def test_resolve_chain_rejects_unknown(monkeypatch):
    monkeypatch.setenv("MEVBOT_CHAIN_REGISTRY_PATH", "config/chains.yaml")
    monkeypatch.setenv("CHAIN_PROFILES_DIR", "config/chains")
    try:
        resolve_chain("EVM:not-a-chain")
    except Exception as e:
        assert "chain_not_found" in str(e) or "unsupported chain key" in str(e)
    else:
        raise AssertionError("expected chain resolution failure")


def test_resolve_chain_uses_mevbot_chain_default(monkeypatch):
    monkeypatch.setenv("MEVBOT_CHAIN_REGISTRY_PATH", "config/chains.yaml")
    monkeypatch.setenv("CHAIN_PROFILES_DIR", "config/chains")
    monkeypatch.delenv("CHAIN", raising=False)
    monkeypatch.delenv("CHAIN_KEY", raising=False)
    monkeypatch.setenv("MEVBOT_CHAIN_DEFAULT", "EVM:bnb-testnet")
    rc = resolve_chain()
    assert rc.chain_key == "EVM:bnb-testnet"
    assert rc.chain_id == 97


def test_resolve_chain_resolves_secret_reference(tmp_path: Path, monkeypatch):
    secrets_path = tmp_path / "secrets.runtime.json"
    secrets_path.write_text(
        '{"rpc_http":{"sepolia":"https://secret-rpc.local"},"rpc_ws":{"sepolia":"wss://secret-rpc.local"}}',
        encoding="utf-8",
    )
    reg_path = tmp_path / "chains.yaml"
    reg_path.write_text(
        '{"chains":{"sepolia":{"chain_id":11155111,"family":"evm","default_rpc_http":"secret://rpc_http/sepolia","default_ws_endpoints":["secret://rpc_ws/sepolia"],"explorer_base_url":"https://sepolia.etherscan.io","native_symbol":"ETH"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MEVBOT_CHAIN_REGISTRY_PATH", str(reg_path))
    monkeypatch.setenv("MEVBOT_SECRETS_PATH", str(secrets_path))
    monkeypatch.setenv("CHAIN_KEY", "EVM:sepolia")
    rc = resolve_chain()
    assert rc.rpc_http == "https://secret-rpc.local"
    assert rc.rpc_ws == "wss://secret-rpc.local"


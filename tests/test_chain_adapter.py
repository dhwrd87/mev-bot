from bot.core.chain_adapter import parse_chain_selection, _rpc_candidates_evm


def test_parse_chain_selection_variants():
    evm = parse_chain_selection("base")
    assert evm.family == "EVM"
    assert evm.chain == "base"

    sol = parse_chain_selection("solana")
    assert sol.family == "SOL"
    assert sol.chain == "solana"

    explicit = parse_chain_selection("EVM:sepolia")
    assert explicit.family == "EVM"
    assert explicit.chain == "sepolia"


def test_parse_chain_selection_rejects_bad_family():
    try:
        parse_chain_selection("ABC:mainnet")
    except ValueError as e:
        assert "family must be EVM or SOL" in str(e)
    else:
        raise AssertionError("expected ValueError for invalid family")


def test_rpc_candidates_dedupes_and_appends_env(monkeypatch):
    monkeypatch.setenv("RPC_HTTP_EXTRA", "https://example-a,https://example-a,https://example-b")
    out = _rpc_candidates_evm("sepolia")
    assert out[0] == "https://ethereum-sepolia-rpc.publicnode.com"
    assert out.count("https://example-a") == 1
    assert out[-1] == "https://example-b"

from bot.core.canonical import (
    canonicalize_family,
    canonicalize_chain,
    infer_network,
    ctx_labels,
)


def test_canonicalize_chain_known_aliases():
    assert canonicalize_chain("eth") == "ethereum"
    assert canonicalize_chain("mainnet") == "ethereum"
    assert canonicalize_chain("bsc") == "bnb"
    assert canonicalize_chain("avax") == "avalanche"
    assert canonicalize_chain("sol") == "solana"
    assert canonicalize_chain("amoy") == "polygon"


def test_canonicalize_family():
    assert canonicalize_family("evm", "ethereum") == "evm"
    assert canonicalize_family("sol", "solana") == "sol"
    assert canonicalize_family("", "base") == "evm"
    assert canonicalize_family(None, "solana") == "sol"


def test_infer_network():
    assert infer_network("ethereum") == "mainnet"
    assert infer_network("sepolia") == "testnet"
    assert infer_network("amoy") == "testnet"
    assert infer_network("solana", "devnet") == "devnet"


def test_ctx_labels_core_and_optional_fields():
    c = ctx_labels(family="evm", chain="sepolia")
    assert c["family"] == "evm"
    assert c["chain"] == "sepolia"
    assert c["network"] == "testnet"
    assert "dex" not in c
    assert "strategy" not in c
    assert "provider" not in c

    c2 = ctx_labels(
        family="evm",
        chain="eth",
        dex="univ3",
        strategy="default",
        provider="publicnode",
    )
    assert c2["family"] == "evm"
    assert c2["chain"] == "ethereum"
    assert c2["network"] == "mainnet"
    assert c2["dex"] == "univ3"
    assert c2["strategy"] == "default"
    assert c2["provider"] == "publicnode"

from bot.core.canonical_chain import (
    canonical_network,
    canonicalize_chain_slug,
    canonicalize_chain_target,
    canonicalize_labels,
)


def test_chain_slug_aliases():
    assert canonicalize_chain_slug("eth") == "ethereum"
    assert canonicalize_chain_slug("sol") == "solana"
    assert canonicalize_chain_slug("bera") == "berachain"


def test_chain_target_canonicalization():
    assert canonicalize_chain_target("base") == "EVM:base"
    assert canonicalize_chain_target("SOL:mainnet-beta") == "SOL:solana"
    assert canonicalize_chain_target("ethereum") == "EVM:ethereum"


def test_canonical_labels_include_network():
    fam, chain, network = canonicalize_labels(family="EVM", chain="sepolia")
    assert fam == "evm"
    assert chain == "sepolia"
    assert network == "testnet"
    assert canonical_network("base") == "mainnet"

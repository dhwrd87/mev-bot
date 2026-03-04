from __future__ import annotations

import os


_CHAIN_ALIASES = {
    "eth": "ethereum",
    "eth-mainnet": "ethereum",
    "mainnet": "ethereum",
    "mainnet-beta": "solana",
    "sol": "solana",
    "sol-mainnet": "solana",
    "sol-mainnet-beta": "solana",
    "solana-devnet": "solana-devnet",
    "sol-devnet": "solana-devnet",
    "base-mainnet": "base",
    "arbitrum-mainnet": "arbitrum",
    "optimism-mainnet": "optimism",
    "polygon-mainnet": "polygon",
    "matic": "polygon",
    "amoy": "polygon",
    "polygon-amoy": "polygon",
    "sepolia-testnet": "sepolia",
    "base-sepolia": "base",
    "arbitrum-sepolia": "arbitrum",
    "optimism-sepolia": "optimism",
    "polygon-amoy-testnet": "polygon",
    "bsc": "bnb",
    "binance": "bnb",
    "avax": "avalanche",
    "bera": "berachain",
}

_KNOWN_EVM_CHAINS = {
    "ethereum",
    "base",
    "arbitrum",
    "optimism",
    "polygon",
    "bnb",
    "avalanche",
    "sepolia",
    "amoy",
    "berachain",
}


def _norm(value: str | None, default: str = "unknown") -> str:
    out = str(value or "").strip().lower()
    return out or default


def canonicalize_chain(chain: str | None) -> str:
    raw = _norm(chain)
    return _CHAIN_ALIASES.get(raw, raw)


def canonicalize_family(family: str | None, chain: str | None = None) -> str:
    c = canonicalize_chain(chain)
    if c == "solana" or c.startswith("solana-"):
        return "sol"
    if c in _KNOWN_EVM_CHAINS:
        return "evm"
    f = _norm(family)
    if f in {"sol", "solana"}:
        return "sol"
    if f in {"evm", "ethereum"}:
        return "evm"
    return "evm" if f not in {"evm", "sol"} else f


def infer_network(chain: str | None, network: str | None = None) -> str:
    explicit = _norm(network, default="")
    if explicit:
        return explicit

    env_override = _norm(os.getenv("CHAIN_NETWORK"), default="")
    if env_override:
        return env_override

    c = canonicalize_chain(chain)
    if c.endswith("-devnet"):
        return "devnet"
    if c in {"sepolia"}:
        return "testnet"
    if c in {"polygon"} and _norm(chain, default="").find("amoy") >= 0:
        return "testnet"
    if c == "solana":
        return _norm(os.getenv("SOLANA_NETWORK", "mainnet"), default="mainnet")
    if c == "berachain":
        return _norm(os.getenv("BERACHAIN_NETWORK", "mainnet"), default="mainnet")
    if c in {"ethereum", "base", "arbitrum", "optimism", "polygon", "bnb", "avalanche"}:
        return "mainnet"
    return "unknown"


def ctx_labels(
    family: str | None = None,
    chain: str | None = None,
    network: str | None = None,
    dex: str | None = None,
    strategy: str | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    ch = canonicalize_chain(chain)
    fam = canonicalize_family(family, ch)
    net = infer_network(chain=chain or ch, network=network)
    out = {
        "family": fam,
        "chain": ch,
        "network": net,
    }
    if dex is not None:
        out["dex"] = _norm(dex)
    if strategy is not None:
        out["strategy"] = _norm(strategy, default="default")
    if provider is not None:
        out["provider"] = _norm(provider)
    return out


# Backward-compatible aliases used across older modules.
def canonical_chain(chain: str | None) -> str:
    return canonicalize_chain(chain)


def canonical_family(family: str | None, chain: str | None = None) -> str:
    return canonicalize_family(family, chain)


def canonical_network(chain: str | None, network: str | None = None) -> str:
    return infer_network(chain, network)


def canonicalize_context(
    family: str | None = None,
    chain: str | None = None,
    network: str | None = None,
    dex: str | None = None,
    strategy: str | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    c = ctx_labels(
        family=family,
        chain=chain,
        network=network,
        dex=dex,
        strategy=strategy,
        provider=provider,
    )
    c.setdefault("dex", _norm(dex))
    c.setdefault("strategy", _norm(strategy, default="default"))
    c.setdefault("provider", _norm(provider))
    return c

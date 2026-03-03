from __future__ import annotations

from bot.core.canonical import (
    canonical_chain as _canonical_chain,
    canonical_family as _canonical_family,
    canonical_network as _canonical_network,
    canonicalize_context,
)


def canonicalize_chain_slug(chain: str | None) -> str:
    return _canonical_chain(chain)


def canonicalize_family(family: str | None, chain: str | None = None) -> str:
    return _canonical_family(family, chain)


def canonical_network(chain: str | None) -> str:
    return _canonical_network(chain)


def canonicalize_labels(*, family: str | None, chain: str | None) -> tuple[str, str, str]:
    c = canonicalize_context(family=family, chain=chain)
    return c["family"], c["chain"], c["network"]


def canonicalize_chain_target(raw_target: str | None, *, allow_unknown: bool = True) -> str:
    raw = str(raw_target or "").strip()
    if not raw:
        return "UNKNOWN" if allow_unknown else ""
    if raw.upper() == "UNKNOWN":
        return "UNKNOWN"

    fam = ""
    chain = raw
    if ":" in raw:
        fam, chain = raw.split(":", 1)
    chain_slug = canonicalize_chain_slug(chain)
    family = canonicalize_family(fam, chain_slug)

    if chain_slug == "unknown" or family == "unknown":
        return "UNKNOWN" if allow_unknown else ""
    if family == "sol":
        return f"SOL:{chain_slug}"
    return f"EVM:{chain_slug}"

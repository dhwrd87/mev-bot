#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Dict, Tuple

import requests

from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.canonical import canonicalize_context
from bot.core.canonical_chain import canonicalize_chain_target
from bot.core.chain_adapter import parse_chain_selection
from bot.core.types_dex import TradeIntent
from ops.operator_state import load_state, update_state


def _parse_labels(s: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not s:
        return out
    for part in s.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def _heartbeat_labels(metrics_text: str) -> list[Tuple[Dict[str, str], float]]:
    rows: list[Tuple[Dict[str, str], float]] = []
    pat = re.compile(r'^mevbot_heartbeat_ts\{([^}]*)\}\s+([0-9.eE+-]+)$')
    for line in metrics_text.splitlines():
        line = line.strip()
        m = pat.match(line)
        if not m:
            continue
        labels = _parse_labels(m.group(1))
        try:
            value = float(m.group(2))
        except Exception:
            value = 0.0
        rows.append((labels, value))
    return rows


def _wait_for_heartbeat(*, metrics_url: str, family: str, chain: str, network: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            text = requests.get(metrics_url, timeout=5).text
            rows = _heartbeat_labels(text)
            for labels, value in rows:
                if (
                    labels.get("family") == family
                    and labels.get("chain") == chain
                    and labels.get("network") == network
                    and value > 0
                ):
                    print(
                        f"heartbeat_ok family={family} chain={chain} network={network} ts={int(value)}",
                        flush=True,
                    )
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(
        f"heartbeat label switch not observed within {timeout_s}s for family={family} chain={chain} network={network}"
    )


def _default_intent(*, family: str, chain: str, network: str, dex: str) -> TradeIntent:
    if family == "sol":
        return TradeIntent(
            family=family,
            chain=chain,
            network=network,
            dex_preference=dex,
            token_in="So11111111111111111111111111111111111111112",
            token_out="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
            amount_in=1_000_000,
            slippage_bps=100,
            ttl_s=30,
            strategy="chain_switch_verifier",
        )
    per_chain = {
        "base": (
            "0x4200000000000000000000000000000000000006",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            1_000_000,
        ),
        "sepolia": (
            "0xfff9976782d46CC05630D1f6EBAB18b2324d6B14",
            "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",
            100_000,
        ),
    }
    token_in, token_out, amount = per_chain.get(
        chain,
        (
            "0x4200000000000000000000000000000000000006",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            1_000_000,
        ),
    )
    return TradeIntent(
        family=family,
        chain=chain,
        network=network,
        dex_preference=dex,
        token_in=token_in,
        token_out=token_out,
        amount_in=amount,
        slippage_bps=100,
        ttl_s=30,
        strategy="chain_switch_verifier",
    )


def _verify_dex_quotes(*, operator_state_path: str, family: str, chain: str, network: str) -> None:
    reg = DEXPackRegistry(operator_state_path=operator_state_path)
    reg.reload(family=family, chain=chain, network=network)
    enabled = reg.enabled_names()
    if not enabled:
        print("dex_verify_skip no_enabled_packs", flush=True)
        return
    failures = []
    for dex in enabled:
        pack = reg.get(dex)
        if pack is None:
            failures.append(f"{dex}:missing_pack")
            continue
        try:
            intent = _default_intent(family=family, chain=chain, network=network, dex=dex)
            q = pack.quote(intent)
            if int(getattr(q, "expected_out", 0) or 0) <= 0:
                raise RuntimeError("expected_out<=0")
            print(f"dex_quote_ok dex={dex} expected_out={q.expected_out}", flush=True)
        except Exception as e:
            failures.append(f"{dex}:{e}")
    if failures:
        raise RuntimeError("dex quote verification failed: " + " | ".join(failures))


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify smooth chain switch: heartbeat labels + DEX quote health")
    ap.add_argument("chain_target", help="Target chain (e.g. EVM:base, sepolia, SOL:solana)")
    ap.add_argument("--operator-state", default="ops/operator_state.json", help="Path to operator_state.json")
    ap.add_argument("--metrics-url", default="http://127.0.0.1:8000/metrics", help="Metrics scrape endpoint")
    ap.add_argument("--timeout-s", type=int, default=30, help="Heartbeat switch timeout")
    ap.add_argument("--set-target", action="store_true", help="Write chain_target + PAUSED + dryrun before verify")
    args = ap.parse_args()

    target = canonicalize_chain_target(args.chain_target)
    if target == "UNKNOWN":
        print(f"invalid chain target: {args.chain_target}", file=sys.stderr)
        return 2

    sel = parse_chain_selection(target)
    ctx = canonicalize_context(family=sel.family.lower(), chain=sel.chain)
    family, chain, network = ctx["family"], ctx["chain"], ctx["network"]

    if args.set_target:
        cur = load_state(args.operator_state)
        patch = {
            "chain_target": target,
            "state": "PAUSED",
            "mode": "dryrun",
        }
        update_state(args.operator_state, {**cur, **patch}, actor="verifier")
        print(f"operator_state_updated target={target} state=PAUSED mode=dryrun", flush=True)

    _wait_for_heartbeat(
        metrics_url=args.metrics_url,
        family=family,
        chain=chain,
        network=network,
        timeout_s=max(5, int(args.timeout_s)),
    )
    _verify_dex_quotes(
        operator_state_path=args.operator_state,
        family=family,
        chain=chain,
        network=network,
    )
    print("chain_switch_verifier_ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

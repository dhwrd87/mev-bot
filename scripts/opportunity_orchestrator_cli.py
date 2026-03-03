#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict

from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.chain_config import get_chain_config
from bot.core.opportunity_engine.types import MarketEvent
from bot.core.router import TradeRouter
from bot.detectors.cross_dex_arb import CrossDexArbDetector
from bot.detectors.routing_improvement import RoutingImprovementDetector
from bot.orchestrator.opportunity_orchestrator import OpportunityOrchestrator


def _sample_events(chain: str, family: str, network: str) -> list[MarketEvent]:
    now = time.time()
    return [
        MarketEvent(
            id="ev1",
            ts=now,
            family=family,
            chain=chain,
            network=network,
            token_in="0x0000000000000000000000000000000000000001",
            token_out="0x0000000000000000000000000000000000000002",
            amount_hint=10**6,
            dex_hint="univ2_default",
            source="cli",
        ),
        MarketEvent(
            id="ev2",
            ts=now,
            family=family,
            chain=chain,
            network=network,
            token_in="0x0000000000000000000000000000000000000002",
            token_out="0x0000000000000000000000000000000000000003",
            amount_hint=5 * 10**5,
            dex_hint="univ3_default",
            source="cli",
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Opportunity orchestrator dryrun harness")
    ap.add_argument("--limit", type=int, default=5, help="max decisions to print")
    args = ap.parse_args()

    cfg = get_chain_config()
    family = "sol" if str(cfg.chain).lower() == "solana" else "evm"
    network = "testnet" if str(cfg.chain).lower() in {"sepolia", "amoy"} else "mainnet"

    registry = DEXPackRegistry()
    registry.reload(family=family, chain=cfg.chain, network=network)
    router = TradeRouter(registry=registry)
    detectors = [CrossDexArbDetector(router), RoutingImprovementDetector(router)]
    orch = OpportunityOrchestrator(router=router, registry=registry, detectors=detectors)

    events = _sample_events(cfg.chain, family, network)
    total = 0
    for ev in events:
        opps = orch.on_event(ev)
        print(f"event={ev.id} opportunities={len(opps)}")
        for opp in opps:
            print("  opp", json.dumps(asdict(opp), sort_keys=True))
            total += 1

    print(f"\nqueued={total}")
    for i in range(max(0, args.limit)):
        d = orch.process_next()
        if d.status == "empty":
            break
        payload = {
            "status": d.status,
            "reason": d.reason,
            "opportunity_id": d.opportunity_id,
            "plan": asdict(d.plan) if d.plan is not None else None,
        }
        print(f"decision[{i}] {json.dumps(payload, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

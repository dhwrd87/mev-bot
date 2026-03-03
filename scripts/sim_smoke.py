#!/usr/bin/env python3
from __future__ import annotations

import time

import asyncio

from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from bot.sim.pre_submit import PreSubmitSimulator
from bot.exec.exact_output import ExactOutputParams
from bot.quote.v3_quoter import V3Quoter, V3QuoteOut


class DummyQuoter(V3Quoter):
    def __init__(self): pass
    def quote_exact_output_single(self, *_args, **_kw):
        return V3QuoteOut(ok=True, amount_in=1500, gas_estimate=150_000)


async def main() -> int:
    w3 = Web3(EthereumTesterProvider())
    sim = PreSubmitSimulator(w3, DummyQuoter())
    now = int(time.time())
    params = ExactOutputParams(
        router="0x0000000000000000000000000000000000009999",
        token_in="0x0000000000000000000000000000000000000001",
        token_out="0x0000000000000000000000000000000000000002",
        fee=3000,
        recipient="0x0000000000000000000000000000000000000003",
        deadline=now + 600,
        amount_out_exact=1000,
        amount_in_max=2000,
        sqrt_price_limit_x96=0,
    )
    sender = w3.eth.accounts[0]
    res = await sim.simulate_swap(params, sender=sender)
    ok = bool(res.ok)
    print(f"sim-smoke ok={ok} reason={res.reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

from bot.core.chain_config import get_chain_config
from bot.storage.pg import (
    get_pool,
    ensure_candidates_table,
    ensure_candidates_outcomes_table,
    fetch_unevaluated_candidates,
    insert_candidate_outcome,
)

log = logging.getLogger("candidate-evaluator")

EVAL_POLL_MS = int(os.getenv("EVAL_POLL_MS", "2000"))
EVAL_TIMEOUT_S = float(os.getenv("EVAL_TIMEOUT_S", "900"))
EVAL_BATCH_LIMIT = int(os.getenv("EVAL_BATCH_LIMIT", "100"))

_RPC_IDX = 0


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            return int(v, 0)
        return int(v)
    except Exception:
        return None


async def _fetch_receipt(sess: aiohttp.ClientSession, tx_hash: str, rpc_urls: list[str]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    global _RPC_IDX
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]}
    for i in range(len(rpc_urls)):
        url = rpc_urls[(_RPC_IDX + i) % len(rpc_urls)]
        try:
            async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(min(1.0 + i * 0.1, 2.0))
                    continue
                data = await resp.json()
                result = data.get("result")
                if result:
                    _RPC_IDX = (_RPC_IDX + 1) % len(rpc_urls)
                    return result, url
        except Exception:
            continue
    return None, None


async def run() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    cfg = get_chain_config()
    rpc_urls = [cfg.rpc_http] + cfg.rpc_http_backups
    if not rpc_urls:
        raise SystemExit("No RPC endpoints configured")

    pool = await get_pool()
    await ensure_candidates_table(pool)
    await ensure_candidates_outcomes_table(pool)

    connector = aiohttp.TCPConnector(keepalive_timeout=30, ttl_dns_cache=120)
    timeout = aiohttp.ClientTimeout(total=8)

    # in-memory schedule/backoff map keyed by candidate id
    state: Dict[int, Dict[str, float]] = {}

    log.info(
        "candidate evaluator start poll_ms=%d timeout_s=%.1f batch_limit=%d rpc_endpoints=%s",
        EVAL_POLL_MS,
        EVAL_TIMEOUT_S,
        EVAL_BATCH_LIMIT,
        rpc_urls,
    )

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
        while True:
            now = time.time()
            rows = await fetch_unevaluated_candidates(pool, EVAL_BATCH_LIMIT)
            if not rows:
                await asyncio.sleep(EVAL_POLL_MS / 1000.0)
                continue

            evaluated = 0
            pending = 0
            timed_out = 0

            for row in rows:
                cid = int(row["id"])
                tx_hash = row["tx_hash"]
                created_at = row["created_at"]
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                elapsed = max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())
                s = state.setdefault(cid, {"next_due": 0.0, "backoff": max(0.5, EVAL_POLL_MS / 1000.0)})
                if now < s["next_due"]:
                    pending += 1
                    continue

                if elapsed >= EVAL_TIMEOUT_S:
                    await insert_candidate_outcome(
                        pool,
                        candidate_id=cid,
                        mined_block=None,
                        success=False,
                        gas_used=None,
                        effective_gas_price=None,
                        observed_after_sec=elapsed,
                    )
                    state.pop(cid, None)
                    evaluated += 1
                    timed_out += 1
                    continue

                receipt, _endpoint = await _fetch_receipt(sess, tx_hash, rpc_urls)
                if receipt is None:
                    # exponential backoff with jitter, capped for low-RPC usage
                    s["backoff"] = min(s["backoff"] * 1.5, 30.0)
                    jitter = random.uniform(0.8, 1.2)
                    s["next_due"] = now + s["backoff"] * jitter
                    pending += 1
                    continue

                mined_block = _to_int(receipt.get("blockNumber"))
                status = _to_int(receipt.get("status"))
                gas_used = _to_int(receipt.get("gasUsed"))
                eff_gas_price = _to_int(receipt.get("effectiveGasPrice"))

                await insert_candidate_outcome(
                    pool,
                    candidate_id=cid,
                    mined_block=mined_block,
                    success=(status == 1),
                    gas_used=gas_used,
                    effective_gas_price=eff_gas_price,
                    observed_after_sec=elapsed,
                )
                state.pop(cid, None)
                evaluated += 1

            log.info(
                "candidate evaluator tick seen=%d evaluated=%d pending=%d timed_out=%d",
                len(rows),
                evaluated,
                pending,
                timed_out,
            )
            await asyncio.sleep(EVAL_POLL_MS / 1000.0)


if __name__ == "__main__":
    asyncio.run(run())

import os
import asyncio
import json
import logging
import time
from typing import Dict, Any, List

import aiohttp
from redis.asyncio import Redis

from bot.core.telemetry import (
    mempool_stream_consume_total,
    mempool_stream_consume_lag_ms,
    rpc_gettx_ok_total,
    rpc_gettx_errors_total,
    dex_tx_detected_total,
)

STREAM = os.getenv("REDIS_STREAM", "mempool:pending:txs")
GROUP = os.getenv("REDIS_GROUP", "mempool")
CONSUMER = os.getenv("REDIS_CONSUMER", "worker-1")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RPC_HTTP = os.getenv("RPC_HTTP", "").strip()  # e.g. https://polygon-rpc.com

# Basic router heuristic (override via DEX_ROUTERS="0x...,0x...")
DEFAULT_ROUTERS = {
    # Uniswap V3 SwapRouter
    "0xe592427a0aece92de3edee1f18e0157c05861564",
    # Sushi
    "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506",
    # QuickSwap
    "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff",
}
DEX_ROUTERS = {
    a.strip().lower() for a in os.getenv("DEX_ROUTERS", "").split(",") if a.strip()
} or DEFAULT_ROUTERS


async def ensure_group(r: Redis) -> None:
    try:
        # MKSTREAM creates stream if missing; ignore if group exists
        await r.xgroup_create(name=STREAM, groupname=GROUP, id="$", mkstream=True)
    except Exception:
        # Likely BUSYGROUP
        pass


async def fetch_tx(sess: aiohttp.ClientSession, tx_hash: str) -> Dict[str, Any] | None:
    if not RPC_HTTP:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionByHash",
        "params": [tx_hash],
    }
    try:
        async with sess.post(RPC_HTTP, json=payload, timeout=5) as resp:
            data = await resp.json()
            if "result" in data and data["result"]:
                rpc_gettx_ok_total.inc()
                return data["result"]
    except Exception:
        rpc_gettx_errors_total.inc()
    return None


def looks_like_dex(tx: Dict[str, Any]) -> bool:
    to = (tx.get("to") or "").lower()
    return to in DEX_ROUTERS


async def run_consumer():
    logging.basicConfig(level=logging.INFO)
    r = Redis.from_url(REDIS_URL)
    await ensure_group(r)

    async with aiohttp.ClientSession() as sess:
        while True:
            # BLOCK for 1000ms for up to 100 entries
            entries = await r.xreadgroup(
                groupname=GROUP,
                consumername=CONSUMER,
                streams={STREAM: ">"},
                count=100,
                block=1000,
            )

            if not entries:
                continue

            for stream, items in entries:
                for entry_id, fields in items:
                    try:
                        tx_hash = fields[b"tx"].decode() if isinstance(fields[b"tx"], bytes) else fields["tx"]
                        ts_pub = float(fields[b"ts"].decode() if isinstance(fields[b"ts"], bytes) else fields["ts"])
                    except Exception:
                        # malformed; ack and move on
                        await r.xack(STREAM, GROUP, entry_id)
                        continue

                    # metrics: lag
                    lag_ms = max(0.0, (time.time() - ts_pub) * 1000.0)
                    mempool_stream_consume_total.labels(stream=STREAM).inc()
                    mempool_stream_consume_lag_ms.observe(lag_ms)

                    # fetch tx body (optional)
                    tx = await fetch_tx(sess, tx_hash)
                    if tx and looks_like_dex(tx):
                        dex_tx_detected_total.inc()

                    # ack
                    await r.xack(STREAM, GROUP, entry_id)


if __name__ == "__main__":
    asyncio.run(run_consumer())

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Set

from bot.storage.pg import (
    get_pool,
    ensure_mempool_pipeline_tables,
    ensure_candidates_table,
    fetch_mempool_tx_since,
    insert_candidate,
)

log = logging.getLogger("candidate-detector")

POLL_SEC = float(os.getenv("CANDIDATE_POLL_SEC", "5"))
BATCH_LIMIT = int(os.getenv("CANDIDATE_BATCH_LIMIT", "1000"))
PRIORITY_FEE_THRESHOLD = int(os.getenv("CANDIDATE_PRIORITY_FEE_WEI", "2000000000"))  # 2 gwei
ALLOWLIST_PATH = os.getenv("CANDIDATE_ALLOWLIST_PATH", "config/allowlist.json")


def _load_allowlist(path: str) -> Set[str]:
    p = Path(path)
    if not p.exists():
        log.warning("allowlist file missing: %s", path)
        return set()
    try:
        raw = json.loads(p.read_text())
        contracts = raw.get("contracts", []) if isinstance(raw, dict) else []
        return {str(c).lower() for c in contracts if isinstance(c, str) and c.strip()}
    except Exception as e:
        log.warning("failed to load allowlist %s: %s", path, e)
        return set()


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


async def run() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    allowlist = _load_allowlist(ALLOWLIST_PATH)
    pool = await get_pool()
    await ensure_mempool_pipeline_tables(pool)
    await ensure_candidates_table(pool)

    cursor = int(os.getenv("CANDIDATE_START_TS_MS", "0"))
    log.info(
        "candidate detector start poll_sec=%.1f batch_limit=%d priority_fee_threshold=%d allowlist_size=%d allowlist_path=%s",
        POLL_SEC,
        BATCH_LIMIT,
        PRIORITY_FEE_THRESHOLD,
        len(allowlist),
        ALLOWLIST_PATH,
    )

    while True:
        rows = await fetch_mempool_tx_since(pool, cursor, BATCH_LIMIT)
        if not rows:
            await asyncio.sleep(POLL_SEC)
            continue

        emitted = 0
        for row in rows:
            tx_hash = row["tx_hash"]
            ts_ms = int(row["last_seen_ts_ms"])
            to_addr = (row["to"] or "").lower() if row["to"] else ""
            max_priority = _to_int(row["max_priority"])

            if max_priority is not None and max_priority >= PRIORITY_FEE_THRESHOLD:
                score = min(1.0, float(max_priority) / float(max(PRIORITY_FEE_THRESHOLD, 1)))
                await insert_candidate(
                    pool,
                    ts_ms=ts_ms,
                    tx_hash=tx_hash,
                    kind="high_priority_fee",
                    score=score,
                    notes={
                        "max_priority": max_priority,
                        "threshold": PRIORITY_FEE_THRESHOLD,
                    },
                )
                emitted += 1

            if to_addr and to_addr in allowlist:
                await insert_candidate(
                    pool,
                    ts_ms=ts_ms,
                    tx_hash=tx_hash,
                    kind="allowlist_hit",
                    score=1.0,
                    notes={"to": to_addr},
                )
                emitted += 1

            if ts_ms > cursor:
                cursor = ts_ms

        log.info("candidate detector tick rows=%d emitted=%d cursor_ts_ms=%d", len(rows), emitted, cursor)


if __name__ == "__main__":
    asyncio.run(run())

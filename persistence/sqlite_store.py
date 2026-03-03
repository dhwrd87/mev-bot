from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PairRecord:
    chain: str
    dex: str
    factory: str
    pool_address: str
    token0: str
    token1: str
    fee_tier: Optional[int] = None
    source_event: str = ""
    discovered_ts: Optional[int] = None


class SqliteStore:
    def __init__(self, path: str = "tmp/discovery.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovered_pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain TEXT NOT NULL,
                    dex TEXT NOT NULL,
                    factory TEXT NOT NULL,
                    pool_address TEXT NOT NULL,
                    token0 TEXT NOT NULL,
                    token1 TEXT NOT NULL,
                    fee_tier INTEGER,
                    source_event TEXT,
                    discovered_ts INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_discovered_pairs
                ON discovered_pairs(chain, dex, pool_address)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discovered_pairs_chain_dex
                ON discovered_pairs(chain, dex, discovered_ts DESC)
                """
            )

    @staticmethod
    def _norm(v: Any) -> str:
        return str(v or "").strip().lower()

    def insert_pair(self, rec: PairRecord) -> bool:
        now = int(time.time())
        discovered_ts = int(rec.discovered_ts if rec.discovered_ts is not None else now)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO discovered_pairs(
                    chain, dex, factory, pool_address, token0, token1, fee_tier, source_event, discovered_ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._norm(rec.chain),
                    self._norm(rec.dex),
                    self._norm(rec.factory),
                    self._norm(rec.pool_address),
                    self._norm(rec.token0),
                    self._norm(rec.token1),
                    int(rec.fee_tier) if rec.fee_tier is not None else None,
                    str(rec.source_event or ""),
                    discovered_ts,
                    now,
                ),
            )
            return int(cur.rowcount or 0) > 0

    def count_pairs(self, *, chain: Optional[str] = None, dex: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) FROM discovered_pairs WHERE 1=1"
        args: list[Any] = []
        if chain:
            q += " AND chain=?"
            args.append(self._norm(chain))
        if dex:
            q += " AND dex=?"
            args.append(self._norm(dex))
        with self._connect() as conn:
            row = conn.execute(q, args).fetchone()
            return int(row[0] if row else 0)

    def last_pair(self, *, chain: str, dex: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT chain,dex,factory,pool_address,token0,token1,fee_tier,source_event,discovered_ts
                FROM discovered_pairs
                WHERE chain=? AND dex=?
                ORDER BY discovered_ts DESC, id DESC
                LIMIT 1
                """,
                (self._norm(chain), self._norm(dex)),
            ).fetchone()
        if not row:
            return None
        return {
            "chain": row[0],
            "dex": row[1],
            "factory": row[2],
            "pool_address": row[3],
            "token0": row[4],
            "token1": row[5],
            "fee_tier": row[6],
            "source_event": row[7],
            "discovered_ts": row[8],
        }

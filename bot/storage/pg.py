# bot/storage/pg.py
import os, json, asyncpg
from typing import Optional, Dict, Any, List

async def get_pool():
    # Try runtime DATABASE_URL first, then fall back to known local defaults.
    db = os.getenv("POSTGRES_DB", "mev_bot")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")

    candidates: List[str] = []
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        candidates.append(explicit)

    user = os.getenv("POSTGRES_USER", "mev_user")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    candidates.append(f"postgresql://{user}:{pwd}@{host}:{port}/{db}")
    # Compatibility fallbacks for stacks initialized with older defaults.
    candidates.append(f"postgresql://mevbot:mevbot_pw@{host}:{port}/mevbot")
    candidates.append("postgresql://mevbot:mevbot_pw@postgres:5432/mevbot")
    candidates.append("postgresql://mev_user:change_me@postgres:5432/mev_bot")

    seen = set()
    deduped = []
    for dsn in candidates:
        if dsn in seen:
            continue
        seen.add(dsn)
        deduped.append(dsn)

    last_err: Optional[Exception] = None
    for dsn in deduped:
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        except Exception as exc:
            last_err = exc
    if last_err:
        raise last_err
    raise RuntimeError("no PostgreSQL DSN candidates available")

async def insert_trade(pool, row: Dict[str, Any]) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO trades (mode, chain, token_in, token_out, pair, size_usd,
                                expected_profit_usd, realized_pnl_usd, gas_used, gas_price_gwei, gas_usd,
                                status, tx_hash, bundle_tag, builder, block_number, inclusion_latency_ms, context)
            VALUES ($1,$2,$3,$4,$5,$6,
                    $7,$8,$9,$10,$11,
                    $12,$13,$14,$15,$16,$17,$18::jsonb)
            RETURNING id
        """,
        row["mode"], row["chain"], row.get("token_in"), row.get("token_out"), row.get("pair"),
        row.get("size_usd"),
        row.get("expected_profit_usd"), row.get("realized_pnl_usd"),
        row.get("gas_used"), row.get("gas_price_gwei"), row.get("gas_usd"),
        row.get("status","submitted"),
        row.get("tx_hash"), row.get("bundle_tag"), row.get("builder"),
        row.get("block_number"), row.get("inclusion_latency_ms"),
        json.dumps(row.get("context", {})))

async def update_trade_outcome(pool, *, id: Optional[int]=None, tx_hash: Optional[str]=None,
                               bundle_tag: Optional[str]=None, status: str,
                               realized_pnl_usd: Optional[float]=None,
                               gas_used: Optional[int]=None, gas_usd: Optional[float]=None,
                               block_number: Optional[int]=None, inclusion_latency_ms: Optional[int]=None,
                               builder: Optional[str]=None):
    async with pool.acquire() as conn:
        where = "id=$9" if id is not None else ("tx_hash=$9" if tx_hash else "bundle_tag=$9")
        key = id if id is not None else (tx_hash if tx_hash else bundle_tag)
        await conn.execute(f"""
            UPDATE trades SET
                status=COALESCE($1,status),
                realized_pnl_usd=COALESCE($2, realized_pnl_usd),
                gas_used=COALESCE($3, gas_used),
                gas_usd=COALESCE($4, gas_usd),
                block_number=COALESCE($5, block_number),
                inclusion_latency_ms=COALESCE($6, inclusion_latency_ms),
                builder=COALESCE($7, builder)
            WHERE {where}
        """, status, realized_pnl_usd, gas_used, gas_usd, block_number, inclusion_latency_ms, builder, key)

async def ensure_mempool_samples_table(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool_samples (
                id BIGSERIAL PRIMARY KEY,
                ts_ms BIGINT NOT NULL,
                tx_hash TEXT UNIQUE NOT NULL,
                to_addr TEXT,
                from_addr TEXT,
                value BIGINT,
                gas BIGINT,
                max_fee_per_gas BIGINT,
                max_priority_fee_per_gas BIGINT,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)

async def upsert_mempool_sample(pool, row: Dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO mempool_samples (
                ts_ms, tx_hash, to_addr, from_addr, value, gas,
                max_fee_per_gas, max_priority_fee_per_gas,
                status, error, updated_at
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now()
            )
            ON CONFLICT (tx_hash) DO UPDATE SET
                ts_ms=EXCLUDED.ts_ms,
                to_addr=EXCLUDED.to_addr,
                from_addr=EXCLUDED.from_addr,
                value=EXCLUDED.value,
                gas=EXCLUDED.gas,
                max_fee_per_gas=EXCLUDED.max_fee_per_gas,
                max_priority_fee_per_gas=EXCLUDED.max_priority_fee_per_gas,
                status=EXCLUDED.status,
                error=EXCLUDED.error,
                updated_at=now()
        """,
        row["ts_ms"], row["tx_hash"], row.get("to_addr"), row.get("from_addr"),
        row.get("value"), row.get("gas"), row.get("max_fee_per_gas"), row.get("max_priority_fee_per_gas"),
        row.get("status","seen"), row.get("error"))

async def ensure_mempool_pipeline_tables(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool_events (
                id BIGSERIAL PRIMARY KEY,
                ts_ms BIGINT NOT NULL,
                tx_hash TEXT NOT NULL,
                source_endpoint TEXT,
                stream_id TEXT NOT NULL UNIQUE,
                raw_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mempool_events_ts_ms ON mempool_events (ts_ms)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mempool_events_tx_hash ON mempool_events (tx_hash)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool_tx (
                tx_hash TEXT PRIMARY KEY,
                chain_id BIGINT,
                "from" TEXT,
                "to" TEXT,
                nonce BIGINT,
                gas BIGINT,
                max_fee BIGINT,
                max_priority BIGINT,
                value BIGINT,
                input_len INTEGER,
                first_seen_ts_ms BIGINT NOT NULL,
                last_seen_ts_ms BIGINT NOT NULL
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mempool_tx_last_seen_ts_ms ON mempool_tx (last_seen_ts_ms)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool_errors (
                id BIGSERIAL PRIMARY KEY,
                ts_ms BIGINT NOT NULL,
                tx_hash TEXT,
                endpoint TEXT,
                error_type TEXT NOT NULL,
                error_msg TEXT,
                http_status INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mempool_errors_ts_ms ON mempool_errors (ts_ms)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mempool_errors_tx_hash ON mempool_errors (tx_hash)")

async def insert_mempool_event(
    pool,
    *,
    ts_ms: int,
    tx_hash: str,
    source_endpoint: Optional[str],
    stream_id: str,
    raw_json: Dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mempool_events (ts_ms, tx_hash, source_endpoint, stream_id, raw_json)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (stream_id) DO NOTHING
            """,
            int(ts_ms),
            tx_hash,
            source_endpoint,
            stream_id,
            json.dumps(raw_json),
        )

async def upsert_mempool_tx(pool, row: Dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mempool_tx (
                tx_hash, chain_id, "from", "to", nonce, gas,
                max_fee, max_priority, value, input_len, first_seen_ts_ms, last_seen_ts_ms
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (tx_hash) DO UPDATE SET
                chain_id=COALESCE(EXCLUDED.chain_id, mempool_tx.chain_id),
                "from"=COALESCE(EXCLUDED."from", mempool_tx."from"),
                "to"=COALESCE(EXCLUDED."to", mempool_tx."to"),
                nonce=COALESCE(EXCLUDED.nonce, mempool_tx.nonce),
                gas=COALESCE(EXCLUDED.gas, mempool_tx.gas),
                max_fee=COALESCE(EXCLUDED.max_fee, mempool_tx.max_fee),
                max_priority=COALESCE(EXCLUDED.max_priority, mempool_tx.max_priority),
                value=COALESCE(EXCLUDED.value, mempool_tx.value),
                input_len=COALESCE(EXCLUDED.input_len, mempool_tx.input_len),
                first_seen_ts_ms=LEAST(mempool_tx.first_seen_ts_ms, EXCLUDED.first_seen_ts_ms),
                last_seen_ts_ms=GREATEST(mempool_tx.last_seen_ts_ms, EXCLUDED.last_seen_ts_ms)
            """,
            row["tx_hash"],
            row.get("chain_id"),
            row.get("from"),
            row.get("to"),
            row.get("nonce"),
            row.get("gas"),
            row.get("max_fee"),
            row.get("max_priority"),
            row.get("value"),
            row.get("input_len"),
            int(row["first_seen_ts_ms"]),
            int(row["last_seen_ts_ms"]),
        )

async def insert_mempool_error(
    pool,
    *,
    ts_ms: int,
    tx_hash: Optional[str],
    endpoint: Optional[str],
    error_type: str,
    error_msg: Optional[str],
    http_status: Optional[int],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mempool_errors (ts_ms, tx_hash, endpoint, error_type, error_msg, http_status)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            int(ts_ms),
            tx_hash,
            endpoint,
            error_type,
            (error_msg or "")[:2000] if error_msg is not None else None,
            http_status,
        )

async def ensure_candidates_table(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id BIGSERIAL PRIMARY KEY,
                ts_ms BIGINT NOT NULL,
                tx_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                notes JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_tx_kind ON candidates (tx_hash, kind)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_created_at ON candidates (created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_ts_ms ON candidates (ts_ms DESC)")
        # Golden-path paper mode columns (kept additive for backward compatibility).
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS chain TEXT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS seen_ts BIGINT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS to_addr TEXT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS decoded_method TEXT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS venue_tag TEXT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS estimated_gas BIGINT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS estimated_edge_bps DOUBLE PRECISION")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS sim_ok BOOLEAN")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS pnl_est DOUBLE PRECISION")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS decision TEXT")
        await conn.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS reject_reason TEXT")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_decision_created ON candidates (decision, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_seen_ts ON candidates (seen_ts DESC)")

async def fetch_mempool_tx_since(pool, since_ts_ms: int, limit: int = 1000) -> List[asyncpg.Record]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tx_hash, chain_id, "from", "to", nonce, gas, max_fee, max_priority, value, input_len, first_seen_ts_ms, last_seen_ts_ms
            FROM mempool_tx
            WHERE last_seen_ts_ms > $1
            ORDER BY last_seen_ts_ms ASC
            LIMIT $2
            """,
            int(since_ts_ms),
            int(limit),
        )
        return list(rows)

async def insert_candidate(
    pool,
    *,
    ts_ms: int,
    tx_hash: str,
    kind: str,
    score: float,
    notes: Dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO candidates (ts_ms, tx_hash, kind, score, notes)
            VALUES ($1,$2,$3,$4,$5::jsonb)
            ON CONFLICT (tx_hash, kind) DO NOTHING
            """,
            int(ts_ms),
            tx_hash,
            kind,
            float(score),
            json.dumps(notes),
        )

async def insert_candidate_golden(pool, row: Dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO candidates (
                ts_ms, tx_hash, kind, score, notes, chain, seen_ts, to_addr,
                decoded_method, venue_tag, estimated_gas, estimated_edge_bps,
                sim_ok, pnl_est, decision, reject_reason
            )
            VALUES (
                $1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16
            )
            ON CONFLICT (tx_hash, kind) DO UPDATE SET
                score=EXCLUDED.score,
                notes=EXCLUDED.notes,
                chain=EXCLUDED.chain,
                seen_ts=EXCLUDED.seen_ts,
                to_addr=EXCLUDED.to_addr,
                decoded_method=EXCLUDED.decoded_method,
                venue_tag=EXCLUDED.venue_tag,
                estimated_gas=EXCLUDED.estimated_gas,
                estimated_edge_bps=EXCLUDED.estimated_edge_bps,
                sim_ok=EXCLUDED.sim_ok,
                pnl_est=EXCLUDED.pnl_est,
                decision=EXCLUDED.decision,
                reject_reason=EXCLUDED.reject_reason
            """,
            int(row["ts_ms"]),
            row["tx_hash"],
            row.get("kind", "golden_path"),
            float(row.get("score", 0.0)),
            json.dumps(row.get("notes", {})),
            row.get("chain"),
            int(row.get("seen_ts", row["ts_ms"])),
            row.get("to_addr"),
            row.get("decoded_method"),
            row.get("venue_tag"),
            int(row.get("estimated_gas", 0)),
            float(row.get("estimated_edge_bps", 0.0)),
            bool(row.get("sim_ok", False)),
            float(row.get("pnl_est", 0.0)),
            row.get("decision"),
            row.get("reject_reason"),
        )

async def ensure_candidates_outcomes_table(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates_outcomes (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                mined_block BIGINT,
                success BOOLEAN,
                gas_used BIGINT,
                effective_gas_price BIGINT,
                observed_after_sec DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT candidates_outcomes_candidate_id_key UNIQUE (candidate_id)
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_outcomes_created_at ON candidates_outcomes (created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_outcomes_candidate_id ON candidates_outcomes (candidate_id)")

async def fetch_unevaluated_candidates(pool, limit: int = 100) -> List[asyncpg.Record]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.tx_hash, c.created_at
            FROM candidates c
            LEFT JOIN candidates_outcomes o ON o.candidate_id = c.id
            WHERE o.candidate_id IS NULL
            ORDER BY c.created_at ASC
            LIMIT $1
            """,
            int(limit),
        )
        return list(rows)

async def insert_candidate_outcome(
    pool,
    *,
    candidate_id: int,
    mined_block: Optional[int],
    success: Optional[bool],
    gas_used: Optional[int],
    effective_gas_price: Optional[int],
    observed_after_sec: float,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO candidates_outcomes (
                candidate_id, mined_block, success, gas_used, effective_gas_price, observed_after_sec
            ) VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (candidate_id) DO NOTHING
            """,
            int(candidate_id),
            mined_block,
            success,
            gas_used,
            effective_gas_price,
            float(observed_after_sec),
        )

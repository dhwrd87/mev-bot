from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

import bot.api.main as api_main


def _dsn() -> str:
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    return (
        f"postgresql://{os.getenv('POSTGRES_USER', 'mev_user')}:{os.getenv('POSTGRES_PASSWORD', 'change_me')}"
        f"@{os.getenv('POSTGRES_HOST', 'postgres')}:{os.getenv('POSTGRES_PORT', '5432')}"
        f"/{os.getenv('POSTGRES_DB', 'mev_bot')}"
    )


@pytest.fixture(scope="module", autouse=True)
def _migrate_once() -> None:
    subprocess.run(["python", "scripts/migrate.py"], check=True)


async def _fetchval(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _fetchrow(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _execute(query: str, *args) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


async def _insert_attempt_record(
    *,
    suffix: str,
    ts_ms: int,
    status: str,
    reason_code: str,
    strategy: str,
    expected_pnl_usd: float,
    gas_estimate: int,
    sim_ok: bool | None,
    sim_error_code: str | None,
    chain: str = "sepolia",
) -> dict[str, str]:
    opp_id = f"itest-opp-{suffix}"
    attempt_id = f"itest-attempt-{suffix}"
    payload_hash = "0x" + (suffix * 4)[:64].ljust(64, "0")
    tx_hash = payload_hash if status in {"SENT", "CONFIRMED", "REVERTED", "DROPPED"} else None
    sim_id = f"itest-sim-{suffix}"
    meta = {
        "strategy": strategy,
        "expected_pnl_usd": expected_pnl_usd,
        "gas_estimate": gas_estimate,
    }
    await _execute(
        """
        INSERT INTO opportunities_audit (
          opportunity_id, input_id, ts_ms, family, chain, network, tx_hash, kind, score, data
        ) VALUES ($1, NULL, $2, 'evm', $3, 'testnet', $4, 'xarb', 1.0, '{}'::jsonb)
        ON CONFLICT (opportunity_id) DO NOTHING
        """,
        opp_id,
        ts_ms,
        chain,
        tx_hash,
    )
    await _execute(
        """
        INSERT INTO opportunity_attempts (
          attempt_id, opportunity_id, ts_ms, mode, lifecycle_state, reason_code, payload_hash, tx_hash, meta, created_at, updated_at
        ) VALUES (
          $1, $2, $3, 'paper', $4::attempt_lifecycle_status, $5, $6, $7, $8::jsonb,
          to_timestamp($9 / 1000.0), to_timestamp($9 / 1000.0)
        )
        ON CONFLICT (attempt_id) DO NOTHING
        """,
        attempt_id,
        opp_id,
        ts_ms,
        status,
        reason_code,
        payload_hash,
        tx_hash,
        json.dumps(meta),
        ts_ms,
    )
    if sim_ok is not None:
        await _execute(
            """
        INSERT INTO opportunity_simulations (
          sim_id, opportunity_id, ts_ms, simulator, sim_ok, pnl_est, error_code, error_message, latency_ms, details, created_at
        ) VALUES (
          $1, $2, $3, 'itest', $4, $5, $6, NULL, 1.0, '{}'::jsonb, to_timestamp(($3::bigint) / 1000.0)
        )
        ON CONFLICT (sim_id) DO NOTHING
        """,
            sim_id,
            opp_id,
            ts_ms,
            sim_ok,
            expected_pnl_usd,
            sim_error_code,
        )
    return {"attempt_id": attempt_id, "opportunity_id": opp_id, "tx_hash": tx_hash or "", "payload_hash": payload_hash}


async def _run_candidate_pipeline_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tx_hash: str) -> dict:
    stream = f"itest:mempool:{uuid.uuid4().hex}"
    group = f"itest-group-{uuid.uuid4().hex[:8]}"
    consumer = f"itest-consumer-{uuid.uuid4().hex[:8]}"
    to_addr = "0x1111111111111111111111111111111111111111"

    op_state_path = tmp_path / "operator_state.json"
    op_state_path.write_text(
        json.dumps(
            {
                "state": "TRADING",
                "mode": "paper",
                "kill_switch": False,
                "last_actor": "itest",
            }
        ),
        encoding="utf-8",
    )
    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(json.dumps({"contracts": [to_addr]}), encoding="utf-8")

    monkeypatch.setenv("REDIS_URL", os.getenv("REDIS_URL", "redis://redis:6379/0"))
    monkeypatch.setenv("REDIS_STREAM", stream)
    monkeypatch.setenv("CANDIDATE_GROUP", group)
    monkeypatch.setenv("CANDIDATE_CONSUMER", consumer)
    monkeypatch.setenv("CHAIN", "sepolia")
    monkeypatch.setenv("CHAIN_FAMILY", "evm")
    monkeypatch.setenv("SIM_MODE", "heuristic")
    monkeypatch.setenv("CANDIDATE_ALLOWLIST_PATH", str(allowlist_path))
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(op_state_path))

    import bot.workers.candidate_pipeline as cp

    cp = importlib.reload(cp)

    async def _fake_fetch_tx(_sess, _tx_hash: str):
        return {
            "hash": _tx_hash,
            "to": to_addr,
            "from": "0x2222222222222222222222222222222222222222",
            "value": hex(10**17),
            "gas": hex(21000),
            "gasPrice": hex(int(20 * 1e9)),
            "nonce": hex(7),
            "input": "0xa9059cbb0000000000000000000000000000000000000000000000000000000000000000",
        }

    monkeypatch.setattr(cp, "_fetch_tx", _fake_fetch_tx)

    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    task = asyncio.create_task(cp._run_pipeline())
    try:
        await asyncio.sleep(0.6)
        await redis.xadd(stream, {"tx": tx_hash, "selector": "0xa9059cbb", "ts_ms": str(int(time.time() * 1000))})

        deadline = time.time() + 12.0
        while time.time() < deadline:
            attempts = await _fetchval("SELECT count(*) FROM opportunity_attempts WHERE payload_hash = $1", tx_hash)
            if int(attempts or 0) > 0:
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("candidate pipeline did not persist opportunity_attempts within timeout")

        row = await _fetchrow(
            """
            SELECT a.attempt_id, a.lifecycle_state::text, COALESCE(a.reason_code, 'none') AS reason
            FROM opportunity_attempts a
            WHERE a.payload_hash=$1
            ORDER BY a.updated_at DESC
            LIMIT 1
            """,
            tx_hash,
        )
        return {
            "attempt_id": str(row[0]),
            "status": str(row[1]),
            "reason": str(row[2]),
        }
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await redis.close()


@pytest.mark.integration
async def test_audit_schema_and_reason_codes_exist():
    names = await _fetchval(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_name IN (
          'reject_reason_codes',
          'opportunity_inputs',
          'opportunities_audit',
          'opportunity_decisions',
          'opportunity_simulations',
          'opportunity_attempts',
          'opportunity_attempt_events'
        )
        """
    )
    assert int(names or 0) == 7
    reason_count = await _fetchval("SELECT count(*) FROM reject_reason_codes")
    assert int(reason_count or 0) >= 10


@pytest.mark.integration
async def test_candidate_pipeline_persists_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    tx_hash = "0x" + "ab" * 32
    result = await _run_candidate_pipeline_once(monkeypatch, tmp_path, tx_hash)

    candidates = await _fetchval("SELECT count(*) FROM candidates WHERE tx_hash = $1", tx_hash)
    decisions = await _fetchval(
        """
        SELECT count(*)
        FROM opportunity_decisions d
        JOIN opportunities_audit o ON o.opportunity_id = d.opportunity_id
        WHERE o.tx_hash = $1
        """,
        tx_hash,
    )
    sims = await _fetchval(
        """
        SELECT count(*)
        FROM opportunity_simulations s
        JOIN opportunities_audit o ON o.opportunity_id = s.opportunity_id
        WHERE o.tx_hash = $1
        """,
        tx_hash,
    )
    attempts = await _fetchval("SELECT count(*) FROM opportunity_attempts WHERE payload_hash = $1", tx_hash)

    assert int(candidates or 0) >= 1
    assert int(decisions or 0) >= 1
    assert int(sims or 0) >= 1
    assert int(attempts or 0) >= 1
    assert result["status"] == "BLOCKED"


@pytest.mark.integration
async def test_deterministic_opportunity_id_dedupes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    tx_hash = "0x" + "cd" * 32
    await _run_candidate_pipeline_once(monkeypatch, tmp_path, tx_hash)
    await _run_candidate_pipeline_once(monkeypatch, tmp_path, tx_hash)

    opps = await _fetchval("SELECT count(*) FROM opportunities_audit WHERE tx_hash = $1", tx_hash)
    decisions = await _fetchval(
        """
        SELECT count(*)
        FROM opportunity_decisions d
        JOIN opportunities_audit o ON o.opportunity_id = d.opportunity_id
        WHERE o.tx_hash = $1
        """,
        tx_hash,
    )
    assert int(opps or 0) == 1
    assert int(decisions or 0) == 1


def _make_api_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_main, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(api_main, "missing_required_env", lambda: [])
    monkeypatch.setenv("WS_POLYGON_1", "")
    monkeypatch.setenv("WS_POLYGON_2", "")
    monkeypatch.setenv("WS_POLYGON_3", "")
    monkeypatch.setenv("WS_ENDPOINTS_EXTRA", "")
    return TestClient(api_main.app)


@pytest.mark.integration
def test_attempts_endpoint_returns_structured_records(monkeypatch: pytest.MonkeyPatch):
    with _make_api_client(monkeypatch) as client:
        r = client.get("/attempts?limit=50")
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert isinstance(body.get("items"), list)
        if body["items"]:
            item = body["items"][0]
            for k in (
                "ts",
                "attempt_id",
                "opportunity_id",
                "payload_hash",
                "strategy",
                "status",
                "reason_code",
                "expected_pnl_usd",
                "gas_estimate",
                "sim_outcome",
                "sim_revert_reason",
                "tx_hash",
                "broadcasted_at",
                "confirmed_at",
                "receipt_block_number",
                "chain",
            ):
                assert k in item


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attempts_endpoint_order_desc(monkeypatch: pytest.MonkeyPatch):
    now_ms = int(time.time() * 1000)
    newer = await _insert_attempt_record(
        suffix=f"{uuid.uuid4().hex[:10]}a",
        ts_ms=now_ms + 1000,
        status="BLOCKED",
        reason_code="operator_not_trading",
        strategy="default",
        expected_pnl_usd=1.25,
        gas_estimate=21000,
        sim_ok=False,
        sim_error_code="sim_failed",
    )
    older = await _insert_attempt_record(
        suffix=f"{uuid.uuid4().hex[:10]}b",
        ts_ms=now_ms,
        status="BLOCKED",
        reason_code="operator_not_trading",
        strategy="default",
        expected_pnl_usd=0.5,
        gas_estimate=21000,
        sim_ok=True,
        sim_error_code=None,
    )
    with _make_api_client(monkeypatch) as client:
        r = client.get("/attempts?limit=5")
        assert r.status_code == 200
        items = r.json().get("items", [])
        ids = [str(i.get("attempt_id")) for i in items]
        assert newer["attempt_id"] in ids and older["attempt_id"] in ids
        assert ids.index(newer["attempt_id"]) < ids.index(older["attempt_id"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attempts_endpoint_includes_strategy_and_sim_fields(monkeypatch: pytest.MonkeyPatch):
    suffix = f"{uuid.uuid4().hex[:12]}c"
    rec = await _insert_attempt_record(
        suffix=suffix,
        ts_ms=int(time.time() * 1000) + 2000,
        status="BLOCKED",
        reason_code="sim_failed",
        strategy="flashloan_arb",
        expected_pnl_usd=12.34,
        gas_estimate=333000,
        sim_ok=False,
        sim_error_code="execution_reverted",
    )
    with _make_api_client(monkeypatch) as client:
        r = client.get("/attempts?limit=20")
        assert r.status_code == 200
        items = r.json().get("items", [])
        row = next((x for x in items if str(x.get("attempt_id")) == rec["attempt_id"]), None)
        assert row is not None
        assert row["strategy"] == "flashloan_arb"
        assert row["status"] == "BLOCKED"
        assert row["reason_code"] == "sim_failed"
        assert float(row["expected_pnl_usd"]) == 12.34
        assert int(float(row["gas_estimate"])) == 333000
        assert row["sim_outcome"] == "FAIL"
        assert row["sim_revert_reason"] == "execution_reverted"
        assert row["chain"] == "sepolia"
        assert row["tx_hash"] is None
        assert row["payload_hash"]


@pytest.mark.integration
def test_candidates_endpoint_includes_audit_fields(monkeypatch: pytest.MonkeyPatch):
    with _make_api_client(monkeypatch) as client:
        r = client.get("/candidates")
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert isinstance(body.get("items"), list)
        if body["items"]:
            item = body["items"][0]
            for k in ("opportunity_id", "decision", "reason_code", "sim_ok"):
                assert k in item


@pytest.mark.integration
def test_paper_report_includes_audit_counts(monkeypatch: pytest.MonkeyPatch):
    with _make_api_client(monkeypatch) as client:
        r = client.get("/paper_report")
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        for k in ("accepted_24h", "rejected_24h", "sims_24h"):
            assert k in body

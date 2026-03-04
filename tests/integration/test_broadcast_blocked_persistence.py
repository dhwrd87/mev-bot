from __future__ import annotations

import json
import os
import subprocess
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest

from bot.exec.orderflow import Endpoint, PrivateOrderflowManager, TxMeta
from ops.metrics import blocked_by_operator_total


async def _db_ready() -> bool:
    try:
        dsn = os.getenv("DATABASE_URL") or (
            f"postgresql://{os.getenv('POSTGRES_USER','mev_user')}:{os.getenv('POSTGRES_PASSWORD','change_me')}"
            f"@{os.getenv('POSTGRES_HOST','postgres')}:{os.getenv('POSTGRES_PORT','5432')}"
            f"/{os.getenv('POSTGRES_DB','mev_bot')}"
        )
        conn = await asyncpg.connect(dsn)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_blocked_attempt_persisted_and_metric_incremented(monkeypatch, tmp_path: Path):
    if not await _db_ready():
        pytest.skip("postgres not available")

    subprocess.run(["python", "scripts/migrate.py"], check=True)

    state_path = tmp_path / "operator_state.json"
    state_path.write_text(
        json.dumps({"state": "PAUSED", "mode": "live", "kill_switch": False, "last_actor": "itest"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))
    monkeypatch.setenv("CHAIN_FAMILY", "evm")

    before = blocked_by_operator_total.labels(
        family="evm",
        chain="sepolia",
        network="testnet",
        scope="submit_private_tx",
        reason="operator_not_trading",
    )._value.get()

    ep = [Endpoint(name="A", url="https://a", kind="rpc")]
    mgr = PrivateOrderflowManager(ep)
    mgr._client.post = AsyncMock()

    res = await mgr.submit_private_tx("0xdeadbeef", TxMeta(chain="sepolia", sim_ok=True))
    assert res["ok"] is False
    assert res["error"] == "operator_not_trading"
    assert mgr._client.post.await_count == 0

    after = blocked_by_operator_total.labels(
        family="evm",
        chain="sepolia",
        network="testnet",
        scope="submit_private_tx",
        reason="operator_not_trading",
    )._value.get()
    assert after >= before + 1

    dsn = os.getenv("DATABASE_URL") or (
        f"postgresql://{os.getenv('POSTGRES_USER','mev_user')}:{os.getenv('POSTGRES_PASSWORD','change_me')}"
        f"@{os.getenv('POSTGRES_HOST','postgres')}:{os.getenv('POSTGRES_PORT','5432')}"
        f"/{os.getenv('POSTGRES_DB','mev_bot')}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        deadline = time.time() + 5.0
        rows = 0
        while time.time() < deadline:
            rows = int(
                await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM opportunity_attempts
                    WHERE lifecycle_state='BLOCKED'
                      AND reason_code='operator_not_trading'
                      AND meta->>'scope'='submit_private_tx'
                    """
                )
                or 0
            )
            if rows > 0:
                break
            await asyncio.sleep(0.2)
        assert rows > 0
    finally:
        await conn.close()

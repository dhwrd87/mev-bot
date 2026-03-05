from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from bot.orchestration.trading_orchestrator import ExecutionResult
from bot.storage.trade_recorder import TradeRecorder


def _dsn() -> str:
    return str(os.getenv("DATABASE_URL", "")).strip() or (
        f"postgresql://{os.getenv('POSTGRES_USER','mev_user')}:{os.getenv('POSTGRES_PASSWORD','change_me')}"
        f"@{os.getenv('POSTGRES_HOST','postgres')}:{os.getenv('POSTGRES_PORT','5432')}"
        f"/{os.getenv('POSTGRES_DB','mev_bot')}"
    )


def _db_ready() -> bool:
    try:
        with psycopg.connect(_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


def _truncate() -> None:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE strategy_performance RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE TABLE trades RESTART IDENTITY CASCADE")


def _fetchone(q: str, p: tuple[Any, ...] = ()) -> dict[str, Any]:
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(q, p)
        r = cur.fetchone()
        return dict(r) if r else {}


def _base_opp() -> dict[str, Any]:
    return {
        "id": f"opp-{uuid.uuid4().hex[:8]}",
        "type": "xarb",
        "detector": "xarb_detector",
        "family": "evm",
        "chain": "sepolia",
        "network": "testnet",
        "token_in": "0xWETH",
        "token_out": "0xUSDC",
        "dex": "uniswap_v3",
        "size_usd": 500.0,
        "approved_size_usd": 500.0,
    }


def _exec(*, tx_hash: str | None = None, executed: bool = True, realized: float = 9.5, gas: float = 1.5, strategy: str = "stealth_default", mode: str = "stealth", error: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        executed=executed,
        mode=mode,
        strategy=strategy,
        reason="ok" if executed else "failed",
        trade_id=None,
        tx_hash=tx_hash,
        bundle_tag=None,
        expected_profit_usd=10.0,
        realized_profit_usd=realized,
        gas_cost_usd=gas,
        slippage_bps=20.0,
        latency_ms=10.0,
        error=error,
        metadata={"relay": "test"},
    )


@pytest.fixture(autouse=True)
def _guard():
    if not _db_ready():
        pytest.skip("postgres not available for trade recorder unit tests")
    _truncate()
    yield
    _truncate()


@pytest.fixture
def recorder() -> TradeRecorder:
    return TradeRecorder(_dsn())


@pytest.mark.asyncio
async def test_tables_created(recorder: TradeRecorder):
    assert recorder is not None
    row = _fetchone("SELECT COUNT(*) AS c FROM pg_tables WHERE tablename IN ('trades','strategy_performance')")
    assert int(row["c"]) == 2


@pytest.mark.asyncio
async def test_trades_table_schema(recorder: TradeRecorder):
    row = _fetchone(
        "SELECT COUNT(*) AS c FROM information_schema.columns WHERE table_name='trades' AND column_name IN ('strategy','mode','net_profit_usd','opportunity_data')"
    )
    assert int(row["c"]) == 4


@pytest.mark.asyncio
async def test_record_trade_success(recorder: TradeRecorder):
    tid = await recorder.record_trade(_base_opp(), {"reason": "ok", "latency_ms": 1}, _exec(tx_hash="0xabc"), {})
    assert tid > 0


@pytest.mark.asyncio
async def test_net_profit_calculation(recorder: TradeRecorder):
    tid = await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaaa", realized=7, gas=2), {})
    row = _fetchone("SELECT net_profit_usd FROM trades WHERE id=%s", (tid,))
    assert float(row["net_profit_usd"]) == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_profit_margin_calculation(recorder: TradeRecorder):
    opp = _base_opp()
    opp["approved_size_usd"] = 200.0
    tid = await recorder.record_trade(opp, {"reason": "ok"}, _exec(tx_hash="0xaab", realized=7, gas=2), {})
    row = _fetchone("SELECT profit_margin_pct FROM trades WHERE id=%s", (tid,))
    assert float(row["profit_margin_pct"]) == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_pair_field_generation(recorder: TradeRecorder):
    tid = await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaac"), {})
    row = _fetchone("SELECT pair FROM trades WHERE id=%s", (tid,))
    assert row["pair"] == "0xWETH-0xUSDC"


@pytest.mark.asyncio
async def test_jsonb_fields_serialization(recorder: TradeRecorder):
    opp = _base_opp()
    opp["payload"] = {"nested": {"a": 1}}
    tid = await recorder.record_trade(opp, {"reason": "ok", "ctx": {"x": 1}}, _exec(tx_hash="0xaad"), {"risk": {"r": 1}})
    row = _fetchone("SELECT risk_state, opportunity_data, decision_context FROM trades WHERE id=%s", (tid,))
    assert isinstance(row["risk_state"], dict)
    assert isinstance(row["opportunity_data"], dict)
    assert isinstance(row["decision_context"], dict)


@pytest.mark.asyncio
async def test_unique_tx_hash_constraint(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaae"), {})
    tid2 = await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaae"), {})
    assert tid2 == -1


@pytest.mark.asyncio
async def test_record_failed_execution(recorder: TradeRecorder):
    tid = await recorder.record_trade(_base_opp(), {"reason": "failed"}, _exec(tx_hash=None, executed=False, error="fail"), {})
    row = _fetchone("SELECT executed, error FROM trades WHERE id=%s", (tid,))
    assert row["executed"] is False


@pytest.mark.asyncio
async def test_strategy_performance_creation(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaaf"), {})
    row = _fetchone("SELECT COUNT(*) AS c FROM strategy_performance")
    assert int(row["c"]) >= 1


@pytest.mark.asyncio
async def test_strategy_performance_aggregation(recorder: TradeRecorder):
    for i in range(5):
        await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash=f"0xb{i}", realized=10, gas=2), {})
    for i in range(2):
        await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash=f"0xc{i}", realized=-3, gas=1, error="fail"), {})
    row = _fetchone("SELECT trades_executed, trades_succeeded, trades_failed, win_rate FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert int(row["trades_executed"]) == 7
    assert int(row["trades_succeeded"]) == 5
    assert int(row["trades_failed"]) == 2
    assert float(row["win_rate"]) == pytest.approx((5 / 7) * 100.0, rel=1e-2)


@pytest.mark.asyncio
async def test_strategy_performance_profit_sum(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xad1", realized=5, gas=1), {})
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xad2", realized=7, gas=2), {})
    row = _fetchone("SELECT gross_profit_usd, net_profit_usd FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert float(row["gross_profit_usd"]) == pytest.approx(12)
    assert float(row["net_profit_usd"]) == pytest.approx(9)


@pytest.mark.asyncio
async def test_strategy_performance_gas_tracking(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xae1", realized=5, gas=1), {})
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xae2", realized=6, gas=3), {})
    row = _fetchone("SELECT gas_cost_usd FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert float(row["gas_cost_usd"]) == pytest.approx(4)


@pytest.mark.asyncio
async def test_strategy_performance_avg_profit(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaf1", realized=5, gas=1), {})
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xaf2", realized=7, gas=3), {})
    row = _fetchone("SELECT avg_profit_per_trade FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert float(row["avg_profit_per_trade"]) == pytest.approx(4)


@pytest.mark.asyncio
async def test_strategy_performance_latency_average(recorder: TradeRecorder):
    e1 = _exec(tx_hash="0xbf1")
    e1.latency_ms = 10
    e2 = _exec(tx_hash="0xbf2")
    e2.latency_ms = 30
    await recorder.record_trade(_base_opp(), {"reason": "ok", "latency_ms": 5}, e1, {})
    await recorder.record_trade(_base_opp(), {"reason": "ok", "latency_ms": 15}, e2, {})
    row = _fetchone("SELECT avg_decision_latency_ms, avg_execution_latency_ms FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert float(row["avg_decision_latency_ms"]) == pytest.approx(10)
    assert float(row["avg_execution_latency_ms"]) == pytest.approx(20)


@pytest.mark.asyncio
async def test_multiple_strategies_isolated(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xcf1", strategy="s1"), {})
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xcf2", strategy="s2"), {})
    row = _fetchone("SELECT COUNT(*) AS c FROM strategy_performance")
    assert int(row["c"]) == 2


@pytest.mark.asyncio
async def test_multiple_chains_isolated(recorder: TradeRecorder):
    a = _base_opp()
    b = _base_opp()
    b["chain"] = "base"
    await recorder.record_trade(a, {"reason": "ok"}, _exec(tx_hash="0xdf1"), {})
    await recorder.record_trade(b, {"reason": "ok"}, _exec(tx_hash="0xdf2"), {})
    row = _fetchone("SELECT COUNT(*) AS c FROM strategy_performance")
    assert int(row["c"]) == 2


@pytest.mark.asyncio
async def test_daily_partitioning(recorder: TradeRecorder):
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xef1"), {})
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("UPDATE strategy_performance SET date=%s", (date.today() - timedelta(days=1),))
    await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xef2"), {})
    row = _fetchone("SELECT COUNT(*) AS c FROM strategy_performance")
    assert int(row["c"]) >= 2


@pytest.mark.asyncio
async def test_error_handling_db_unavailable():
    bad = TradeRecorder(_dsn())
    bad.database_url = "postgresql://invalid:invalid@127.0.0.1:1/invalid"
    tid = await bad.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash="0xff1"), {})
    assert tid == -1


@pytest.mark.asyncio
async def test_concurrent_writes(recorder: TradeRecorder):
    async def _write(i: int):
        await recorder.record_trade(_base_opp(), {"reason": "ok"}, _exec(tx_hash=f"0xcon{i}"), {})

    await asyncio.gather(*[_write(i) for i in range(10)])
    row = _fetchone("SELECT COUNT(*) AS c FROM trades")
    assert int(row["c"]) == 10

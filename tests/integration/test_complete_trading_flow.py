from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest
from prometheus_client import generate_latest
from psycopg.rows import dict_row
from redis.asyncio import Redis

from bot.orchestration.trading_orchestrator import ExecutionResult, TradingOrchestrator
from bot.storage.trade_recorder import TradeRecorder
from bot.workers.opportunity_processor import OpportunityProcessor


def _dsn() -> str:
    return str(os.getenv("DATABASE_URL", "")).strip() or (
        f"postgresql://{os.getenv('POSTGRES_USER','mev_user')}:{os.getenv('POSTGRES_PASSWORD','change_me')}"
        f"@{os.getenv('POSTGRES_HOST','postgres')}:{os.getenv('POSTGRES_PORT','5432')}"
        f"/{os.getenv('POSTGRES_DB','mev_bot')}"
    )


def _redis_url() -> str:
    return str(os.getenv("REDIS_URL", "redis://redis:6379/0")).strip()


def _db_ready() -> bool:
    try:
        with psycopg.connect(_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


async def _redis_ready() -> bool:
    try:
        r = Redis.from_url(_redis_url())
        await r.ping()
        await r.close()
        return True
    except Exception:
        return False


def _truncate():
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE strategy_performance RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE TABLE trades RESTART IDENTITY CASCADE")


def _fetchone(q: str, p=()):
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(q, p)
        r = cur.fetchone()
        return dict(r) if r else {}


@pytest.fixture(autouse=True)
def _cleanup():
    if _db_ready():
        _truncate()
    yield
    if _db_ready():
        _truncate()


@pytest.fixture
def opportunity():
    return {
        "id": f"opp-{uuid.uuid4().hex[:8]}",
        "family": "evm",
        "chain": "sepolia",
        "network": "testnet",
        "type": "xarb",
        "detector": "xarb_detector",
        "expected_profit_usd": 10.0,
        "size_usd": 500.0,
        "gas_gwei": 30.0,
        "estimated_slippage": 0.003,
        "token_in": "0xWETH",
        "token_out": "0xUSDC",
        "dex": "uniswap_v3",
    }


def _build_orchestrator(state_file: Path) -> TradingOrchestrator:
    stealth = AsyncMock()
    stealth.execute.return_value = Mock(
        success=True,
        tx_hash="0xint_stealth",
        slippage=0.002,
        notes={"realized_profit_usd": 9.5, "gas_cost_usd": 1.5, "relay": "test"},
    )
    hunter = AsyncMock()
    hunter.execute.return_value = Mock(
        success=True,
        tx_hash="0xint_hunter",
        slippage=0.0,
        notes={"realized_profit_usd": 8.0, "gas_cost_usd": 2.0, "bundle_tag": "b-int"},
    )
    risk = Mock()
    risk.approve_trade = Mock(return_value=(True, "approved", 500.0))
    risk.position_cap = Mock(side_effect=lambda s: s)
    risk.should_execute = Mock(return_value=(True, "ok"))
    risk.record_trade_result = Mock()
    risk.daily_pnl = 0.0
    recorder = AsyncMock()
    recorder.record_trade = AsyncMock(return_value=1)
    return TradingOrchestrator(
        settings=Mock(),
        stealth_strategy=stealth,
        hunter_strategy=hunter,
        risk_manager=risk,
        trade_recorder=recorder,
        operator_state_path=str(state_file),
    )


@pytest.mark.asyncio
async def test_opportunity_to_database_flow(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    execution = ExecutionResult(
        executed=True,
        mode="stealth",
        strategy="stealth_default",
        reason="ok",
        trade_id=None,
        tx_hash=f"0x{uuid.uuid4().hex[:24]}",
        bundle_tag=None,
        expected_profit_usd=10.0,
        realized_profit_usd=9.5,
        gas_cost_usd=1.5,
        slippage_bps=20.0,
        latency_ms=10.0,
        error=None,
        metadata={},
    )
    tid = await recorder.record_trade(opportunity, {"reason": "ok", "latency_ms": 1.0}, execution, {})
    assert tid > 0
    row = _fetchone("SELECT COUNT(*) AS c FROM trades")
    assert int(row["c"]) == 1


@pytest.mark.asyncio
async def test_redis_stream_to_orchestrator():
    if not await _redis_ready():
        pytest.skip("redis not available")
    stream = f"itest:stream:{uuid.uuid4().hex}"
    group = f"g-{uuid.uuid4().hex[:8]}"

    class _Orch:
        def __init__(self):
            self.calls = 0

        async def handle_opportunity(self, _o):
            self.calls += 1
            return {"outcome": "success"}

    class _Detector:
        def detect(self, _tx):
            return [{"type": "xarb", "expected_profit_usd": 2, "size_usd": 100, "token_in": "a", "token_out": "b", "dex": "u", "detector": "d"}]

    orch = _Orch()
    p = OpportunityProcessor(redis_url=_redis_url(), stream=stream, group=group, orchestrator=orch, detectors=[_Detector()])
    await p._ensure_group()
    try:
        await p.redis.xadd(stream, {"hash": "0x1", "chain": "sepolia", "family": "evm"})
        res = await p.redis.xreadgroup(groupname=group, consumername=p.consumer, streams={stream: ">"}, count=1, block=1000)
        entry_id, fields = res[0][1][0]
        await p._process_entry(entry_id=str(entry_id), fields=fields)
        assert orch.calls == 1
    finally:
        await p.redis.delete(stream)
        await p.shutdown()


@pytest.mark.asyncio
async def test_multiple_opportunities_sequential(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    for i in range(10):
        o = dict(opportunity)
        o["id"] = f"{opportunity['id']}-{i}"
        e = ExecutionResult(True, "stealth", "stealth_default", "ok", None, f"0x{i:02x}", None, 3, 2, 1, 10, 5, None, {})
        await recorder.record_trade(o, {"reason": "ok", "latency_ms": 1}, e, {})
    row = _fetchone("SELECT COUNT(*) AS c FROM trades")
    assert int(row["c"]) == 10


@pytest.mark.asyncio
async def test_different_strategies_different_chains(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    o1 = dict(opportunity)
    o2 = dict(opportunity)
    o2["chain"] = "base"
    e1 = ExecutionResult(True, "stealth", "stealth_default", "ok", None, "0x11", None, 2, 1, 1, 10, 5, None, {})
    e2 = ExecutionResult(True, "hunter", "hunter_backrun", "ok", None, "0x22", "b", 4, 3, 1, 8, 5, None, {})
    await recorder.record_trade(o1, {"reason": "ok", "latency_ms": 1}, e1, {})
    await recorder.record_trade(o2, {"reason": "ok", "latency_ms": 1}, e2, {})
    row = _fetchone("SELECT COUNT(*) AS c FROM strategy_performance")
    assert int(row["c"]) == 2


@pytest.mark.asyncio
async def test_metrics_emission_integration(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)
    await orch.handle_opportunity(opportunity)
    txt = generate_latest().decode("utf-8", errors="ignore")
    assert "strategy_decisions_total" in txt or "mevbot_strategy_decisions_total" in txt


@pytest.mark.asyncio
async def test_risk_rejection_no_database_record(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)
    orch.risk_manager.approve_trade = Mock(return_value=(False, "risk_blocked", 0.0))
    r = await orch.handle_opportunity(opportunity)
    assert r.executed is False
    orch.trade_recorder.record_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_state_integration(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"true","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)
    r = await orch.handle_opportunity(opportunity)
    assert r.mode == "none"
    assert r.reason == "operator_paused"


@pytest.mark.asyncio
async def test_concurrent_opportunity_processing(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)

    async def _run(i: int):
        o = dict(opportunity)
        o["id"] = f"{opportunity['id']}-{i}"
        return await orch.handle_opportunity(o)

    out = await asyncio.gather(*[_run(i) for i in range(20)])
    assert len(out) == 20
    assert all(x.executed for x in out)


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    with psycopg.connect(_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO trades (family, chain, mode, strategy, executed, status, params) "
                "VALUES ('evm','sepolia','stealth','s',true,'success','{}'::jsonb)"
            )
            raise RuntimeError("force rollback")
        except RuntimeError:
            conn.rollback()
    row = _fetchone("SELECT COUNT(*) AS c FROM trades")
    assert int(row["c"]) == 0


@pytest.mark.asyncio
async def test_latency_tracking_realistic(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)
    started = time.perf_counter()
    r = await orch.handle_opportunity(opportunity)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert r.latency_ms <= elapsed_ms + 50.0
    assert r.latency_ms < 1000.0


@pytest.mark.asyncio
async def test_full_stack_smoke(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    orch = _build_orchestrator(p)
    r = await orch.handle_opportunity(opportunity)
    assert r.executed is True


@pytest.mark.asyncio
async def test_strategy_performance_win_rate_calculation(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    for i in range(4):
        e = ExecutionResult(True, "stealth", "s", "ok", None, f"0xw{i}", None, 3, 2, 1, 10, 5, None, {})
        await recorder.record_trade(opportunity, {"reason": "ok", "latency_ms": 1}, e, {})
    for i in range(1):
        e = ExecutionResult(True, "stealth", "s", "ok", None, f"0xl{i}", None, 1, -1, 1, 10, 5, "fail", {})
        await recorder.record_trade(opportunity, {"reason": "ok", "latency_ms": 1}, e, {})
    row = _fetchone("SELECT win_rate FROM strategy_performance ORDER BY id DESC LIMIT 1")
    assert float(row["win_rate"]) == pytest.approx(80.0, rel=1e-3)


@pytest.mark.asyncio
async def test_gas_cost_tracking(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    e = ExecutionResult(True, "stealth", "s", "ok", None, "0xgas", None, 3, 2, 1.7, 10, 5, None, {})
    await recorder.record_trade(opportunity, {"reason": "ok", "latency_ms": 1}, e, {})
    row = _fetchone("SELECT gas_cost_usd FROM trades LIMIT 1")
    assert float(row["gas_cost_usd"]) == pytest.approx(1.7)


@pytest.mark.asyncio
async def test_trade_metadata_roundtrip(opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(_dsn())
    e = ExecutionResult(True, "stealth", "s", "ok", None, "0xmeta", None, 3, 2, 1, 10, 5, None, {"relay": "x"})
    await recorder.record_trade(opportunity, {"reason": "ok", "latency_ms": 1, "ctx": {"a": 1}}, e, {"daily_pnl": 2})
    row = _fetchone("SELECT risk_state, decision_context, execution_metadata FROM trades LIMIT 1")
    assert row["risk_state"]["daily_pnl"] == 2
    assert row["decision_context"]["ctx"]["a"] == 1
    assert row["execution_metadata"]["relay"] == "x"


@pytest.mark.asyncio
async def test_error_recovery(opportunity, tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    opportunity["force_stealth"] = True
    orch = _build_orchestrator(p)
    orch.stealth.execute.side_effect = [RuntimeError("transient"), Mock(success=True, tx_hash="0xok", slippage=0.1, notes={"realized_profit_usd": 1, "gas_cost_usd": 1})]
    r1 = await orch.handle_opportunity(opportunity)
    r2 = await orch.handle_opportunity(opportunity)
    assert r1.executed is False
    assert r2.executed is True

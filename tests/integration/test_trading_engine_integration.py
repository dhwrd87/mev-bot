from __future__ import annotations

import os
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest
from prometheus_client import generate_latest
from psycopg.rows import dict_row
from redis.asyncio import Redis

from bot.core.telemetry_trading import risk_rejections_total
from bot.orchestration.trading_orchestrator import ExecutionResult, TradingOrchestrator
from bot.storage.trade_recorder import TradeRecorder
from bot.strategy.hunter import HunterStrategy
from bot.strategy.stealth import StealthStrategy
from bot.workers.opportunity_processor import (
    OpportunityProcessor,
    opportunities_detected_total,
    opportunities_processed_total,
)


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


def _truncate_trade_tables() -> None:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE strategy_performance RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE TABLE trades RESTART IDENTITY CASCADE")


def _fetchone(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else {}


def _fetchall(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return [dict(r) for r in (cur.fetchall() or [])]


@pytest.fixture
def test_opportunity() -> dict[str, Any]:
    return {
        "id": "test_opp_001",
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


@pytest.fixture
def operator_state_file(tmp_path: Path) -> Path:
    p = tmp_path / "operator_state_runtime.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    return p


@pytest.fixture
def test_orchestrator(test_opportunity: dict[str, Any], operator_state_file: Path):
    mock_stealth = AsyncMock(spec=StealthStrategy)
    mock_stealth.execute.return_value = Mock(
        success=True,
        tx_hash="0xtest123",
        slippage=0.002,
        sandwiched=False,
        notes={"profit_usd": 9.5, "gas_cost_usd": 1.5, "relay": "test"},
    )

    mock_hunter = AsyncMock(spec=HunterStrategy)
    mock_hunter.execute.return_value = Mock(
        success=True,
        tx_hash="0xhunter123",
        slippage=0.0,
        sandwiched=False,
        notes={"profit_usd": 8.0, "gas_cost_usd": 2.0, "bundle_tag": "b-1"},
    )

    mock_risk = Mock()
    mock_risk.approve_trade = Mock(return_value=(True, "approved", 500.0))
    mock_risk.record_trade_result = Mock()
    mock_risk.get_current_state = Mock(return_value={"exposure_usd": 100.0})
    mock_risk.daily_pnl = 0.0

    mock_recorder = AsyncMock()
    mock_recorder.record_trade = AsyncMock(return_value=1)

    orch = TradingOrchestrator(
        settings=Mock(),
        stealth_strategy=mock_stealth,
        hunter_strategy=mock_hunter,
        risk_manager=mock_risk,
        trade_recorder=mock_recorder,
        operator_state_path=str(operator_state_file),
    )
    return orch


@pytest.fixture(autouse=True)
def _db_cleanup():
    if _db_ready():
        _truncate_trade_tables()
    yield
    if _db_ready():
        _truncate_trade_tables()


@pytest.mark.asyncio
async def test_opportunity_to_trade_flow():
    if not _db_ready():
        pytest.skip("postgres not available")
    if not await _redis_ready():
        pytest.skip("redis not available")

    recorder = TradeRecorder(database_url=_dsn())
    stream = f"itest:opp:{uuid.uuid4().hex}"
    group = f"itest_group_{uuid.uuid4().hex[:8]}"

    class _Detector:
        def detect(self, _tx: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                {
                    "id": "test_opp_001",
                    "type": "xarb",
                    "expected_profit_usd": 10.0,
                    "size_usd": 500.0,
                    "token_in": "0xWETH",
                    "token_out": "0xUSDC",
                    "dex": "uniswap_v3",
                    "detector": "xarb_detector",
                }
            ]

    class _RecordingOrchestrator:
        async def handle_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
            execution = ExecutionResult(
                executed=True,
                mode="stealth",
                strategy="stealth_private",
                reason="gas_spike",
                trade_id=None,
                tx_hash="0xtest123",
                bundle_tag=None,
                expected_profit_usd=float(opportunity.get("expected_profit_usd", 0.0)),
                realized_profit_usd=8.5,
                gas_cost_usd=1.5,
                slippage_bps=20.0,
                latency_ms=12.0,
                error=None,
                metadata={"relay": "test"},
            )
            decision = {"mode": execution.mode, "strategy": execution.strategy, "reason": execution.reason, "latency_ms": 5.0}
            await recorder.record_trade(opportunity, decision, execution, {"daily_pnl": 8.5})
            return {"outcome": "success"}

    processor = OpportunityProcessor(
        redis_url=_redis_url(),
        stream=stream,
        group=group,
        orchestrator=_RecordingOrchestrator(),
        detectors=[_Detector()],
    )
    await processor._ensure_group()
    try:
        await processor.redis.xadd(
            stream,
            {
                "hash": "0xfeedbeef",
                "chain": "sepolia",
                "family": "evm",
                "from": "0xabc",
                "to": "0xdef",
                "value": "1000",
                "data": "0x",
                "gas_price": "100",
            },
        )
        response = await processor.redis.xreadgroup(
            groupname=group,
            consumername=processor.consumer,
            streams={stream: ">"},
            count=1,
            block=1000,
        )
        assert response
        entry_id, fields = response[0][1][0]
        started = time.perf_counter()
        await processor._process_entry(entry_id=entry_id.decode() if isinstance(entry_id, (bytes, bytearray)) else str(entry_id), fields=fields)
        total_ms = (time.perf_counter() - started) * 1000.0

        row = _fetchone("SELECT * FROM trades WHERE opportunity_id = %s", ("test_opp_001",))
        assert row
        assert row["mode"] == "stealth"
        assert row["strategy"] == "stealth_private"
        assert float(row["expected_profit_usd"]) == pytest.approx(10.0)
        assert opportunities_detected_total.labels("evm", "sepolia", "xarb", "xarb_detector")._value.get() >= 1
        assert opportunities_processed_total.labels("evm", "sepolia", "xarb", "success")._value.get() >= 1
        assert total_ms < 1000.0
    finally:
        await processor.redis.delete(stream)
        await processor.shutdown()


@pytest.mark.asyncio
async def test_strategy_selection_gas_spike(test_orchestrator, test_opportunity):
    test_opportunity["gas_gwei"] = 150
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.mode == "stealth"
    assert result.strategy == "stealth_private"
    assert result.reason == "gas_spike"


@pytest.mark.asyncio
async def test_strategy_selection_slippage(test_orchestrator, test_opportunity):
    test_opportunity["estimated_slippage"] = 0.01
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.mode == "stealth"
    assert "exact_output" in result.strategy
    assert result.reason == "high_slippage_risk"


@pytest.mark.asyncio
async def test_strategy_selection_sniper_detection(test_orchestrator, test_opportunity):
    test_opportunity["detected_snipers"] = 3
    test_opportunity["vulnerable_flow"] = True
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.mode == "hunter"
    assert result.reason == "sniper_opportunity"


@pytest.mark.asyncio
async def test_risk_rejection(test_orchestrator, test_opportunity):
    test_opportunity["force_stealth"] = True
    before = risk_rejections_total.labels("evm", "sepolia", "stealth", "daily_loss_limit")._value.get()
    test_orchestrator.risk_manager.approve_trade = Mock(return_value=(False, "daily_loss_limit", 0.0))
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.executed is False
    assert "daily_loss_limit" in result.reason
    test_orchestrator.trade_recorder.record_trade.assert_not_awaited()
    after = risk_rejections_total.labels("evm", "sepolia", "stealth", "daily_loss_limit")._value.get()
    assert after >= before + 1


@pytest.mark.asyncio
async def test_operator_paused(test_orchestrator, test_opportunity, operator_state_file: Path):
    operator_state_file.write_text('{"paused":"true","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.mode == "none"
    assert result.reason == "operator_paused"
    assert result.executed is False


@pytest.mark.asyncio
async def test_kill_switch(test_orchestrator, test_opportunity, operator_state_file: Path):
    operator_state_file.write_text('{"paused":"false","kill_switch":"true","state":"TRADING","mode":"paper"}', encoding="utf-8")
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.mode == "none"
    assert result.reason == "kill_switch_active"
    assert result.executed is False


@pytest.mark.asyncio
async def test_trade_recording(test_opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(database_url=_dsn())
    execution = ExecutionResult(
        executed=True,
        mode="stealth",
        strategy="stealth_default",
        reason="default_safe",
        trade_id=None,
        tx_hash="0xtrade001",
        bundle_tag=None,
        expected_profit_usd=10.0,
        realized_profit_usd=9.5,
        gas_cost_usd=1.5,
        slippage_bps=30.0,
        latency_ms=15.0,
        error=None,
        metadata={"relay": "flashbots"},
    )
    decision = {"mode": "stealth", "strategy": "stealth_default", "reason": "default_safe", "latency_ms": 5.0}
    trade_id = await recorder.record_trade(test_opportunity, decision, execution, {"daily_pnl": 8.0})
    assert trade_id > 0

    row = _fetchone("SELECT * FROM trades WHERE id = %s", (trade_id,))
    assert row["opportunity_id"] == "test_opp_001"
    assert row["opportunity_type"] == "xarb"
    assert row["detector"] == "xarb_detector"
    assert row["family"] == "evm"
    assert row["chain"] == "sepolia"
    assert row["network"] == "testnet"
    assert row["mode"] == "stealth"
    assert row["strategy"] == "stealth_default"
    assert row["decision_reason"] == "default_safe"
    assert row["executed"] is True
    assert row["tx_hash"] == "0xtrade001"
    assert float(row["expected_profit_usd"]) == pytest.approx(10.0)
    assert float(row["realized_profit_usd"]) == pytest.approx(9.5)
    assert float(row["gas_cost_usd"]) == pytest.approx(1.5)
    assert float(row["net_profit_usd"]) == pytest.approx(8.0)
    assert row["created_at"] is not None


@pytest.mark.asyncio
async def test_strategy_performance_aggregation(test_opportunity):
    if not _db_ready():
        pytest.skip("postgres not available")
    recorder = TradeRecorder(database_url=_dsn())

    for i in range(5):
        execution = ExecutionResult(
            executed=True,
            mode="stealth",
            strategy="stealth_default",
            reason="ok",
            trade_id=None,
            tx_hash=f"0xsucc{i}",
            bundle_tag=None,
            expected_profit_usd=10.0,
            realized_profit_usd=10.0,
            gas_cost_usd=2.0,
            slippage_bps=20.0,
            latency_ms=10.0,
            error=None,
            metadata={},
        )
        await recorder.record_trade(test_opportunity, {"reason": "ok", "latency_ms": 2.0}, execution, {})

    for i in range(2):
        execution = ExecutionResult(
            executed=True,
            mode="stealth",
            strategy="stealth_default",
            reason="failed",
            trade_id=None,
            tx_hash=f"0xfail{i}",
            bundle_tag=None,
            expected_profit_usd=5.0,
            realized_profit_usd=-3.0,
            gas_cost_usd=1.0,
            slippage_bps=30.0,
            latency_ms=12.0,
            error="failed",
            metadata={},
        )
        await recorder.record_trade(test_opportunity, {"reason": "failed", "latency_ms": 3.0}, execution, {})

    row = _fetchone(
        """
        SELECT *
        FROM strategy_performance
        WHERE date = %s AND family='evm' AND chain='sepolia' AND mode='stealth' AND strategy='stealth_default'
        """,
        (date.today(),),
    )
    assert int(row["opportunities_total"]) >= 7
    assert int(row["trades_executed"]) == 7
    assert int(row["trades_succeeded"]) == 5
    assert int(row["trades_failed"]) == 2
    assert float(row["win_rate"]) == pytest.approx((5 / 7) * 100.0, rel=1e-2)
    assert float(row["gross_profit_usd"]) == pytest.approx(44.0)  # 5*10 + 2*(-3)
    assert float(row["gas_cost_usd"]) == pytest.approx(12.0)      # 5*2 + 2*1
    assert float(row["net_profit_usd"]) == pytest.approx(32.0)


@pytest.mark.asyncio
async def test_metrics_emission(test_orchestrator, test_opportunity):
    result = await test_orchestrator.handle_opportunity(test_opportunity)
    assert result.executed is True

    metrics_text = generate_latest().decode("utf-8", errors="ignore")
    assert ("strategy_decisions_total" in metrics_text) or ("mevbot_strategy_decisions_total" in metrics_text)
    assert ("executions_attempted_total" in metrics_text) or ("mevbot_executions_attempted_total" in metrics_text)
    assert ("executions_completed_total" in metrics_text) or ("mevbot_executions_completed_total" in metrics_text)
    assert ("trade_realized_profit_usd_bucket" in metrics_text) or ("mevbot_trade_realized_profit_usd_bucket" in metrics_text)
    assert ("cumulative_pnl_usd" in metrics_text) or ("mevbot_cumulative_pnl_usd" in metrics_text)

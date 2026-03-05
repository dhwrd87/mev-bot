from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from bot.orchestration.trading_orchestrator import TradingOrchestrator


def _base_opp() -> dict:
    return {
        "id": "opp-1",
        "family": "evm",
        "chain": "sepolia",
        "network": "testnet",
        "type": "xarb",
        "detector": "xarb_detector",
        "expected_profit_usd": 2.0,
        "size_usd": 500.0,
        "gas_gwei": 30.0,
        "estimated_slippage": 0.003,
        "token_in": "0xWETH",
        "token_out": "0xUSDC",
        "dex": "uniswap_v3",
    }


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    p = tmp_path / "state.json"
    p.write_text('{"paused":"false","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    return p


@pytest.fixture
def orchestrator(state_file: Path) -> TradingOrchestrator:
    stealth = AsyncMock()
    stealth.execute.return_value = Mock(
        success=True,
        tx_hash="0xstealth",
        slippage=0.002,
        notes={"realized_profit_usd": 9.5, "gas_cost_usd": 1.5, "relay": "test"},
    )
    hunter = AsyncMock()
    hunter.execute.return_value = Mock(
        success=True,
        tx_hash="0xhunter",
        slippage=0.001,
        notes={"realized_profit_usd": 8.0, "gas_cost_usd": 2.0, "bundle_tag": "b-1"},
    )
    risk = Mock()
    risk.approve_trade = Mock(return_value=(True, "approved", 500.0))
    risk.should_execute = Mock(return_value=(True, "ok"))
    risk.position_cap = Mock(side_effect=lambda s: s)
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
async def test_gas_spike_triggers_stealth(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["gas_gwei"] = 150
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "stealth"
    assert r.strategy == "stealth_private"
    assert r.reason == "gas_spike"


@pytest.mark.asyncio
async def test_high_slippage_triggers_exact_output(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["estimated_slippage"] = 0.01
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "stealth"
    assert "exact_output" in r.strategy
    assert r.reason == "high_slippage_risk"


@pytest.mark.asyncio
async def test_sniper_detection_triggers_hunter(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["detected_snipers"] = 3
    opp["vulnerable_flow"] = True
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "hunter"
    assert r.reason == "sniper_opportunity"


@pytest.mark.asyncio
async def test_new_token_triggers_stealth(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["token_age_hours"] = 12
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "stealth"
    assert r.strategy == "stealth_private"
    assert r.reason == "new_token"


@pytest.mark.asyncio
async def test_arbitrage_high_profit_triggers_hunter(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["type"] = "triarb"
    opp["expected_profit_usd"] = 10
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "hunter"
    assert r.reason == "arbitrage"


@pytest.mark.asyncio
async def test_low_profit_defaults_stealth(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["type"] = "xarb"
    opp["expected_profit_usd"] = 2
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "stealth"
    assert r.reason == "default_safe"


@pytest.mark.asyncio
async def test_force_stealth_overrides(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["force_stealth"] = True
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "stealth"
    assert r.reason == "forced"


@pytest.mark.asyncio
async def test_force_hunter_overrides(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["force_hunter"] = True
    r = await orchestrator.handle_opportunity(opp)
    assert r.mode == "hunter"
    assert r.reason == "forced"


@pytest.mark.asyncio
async def test_operator_paused_blocks(orchestrator: TradingOrchestrator, state_file: Path):
    state_file.write_text('{"paused":"true","kill_switch":"false","state":"TRADING","mode":"paper"}', encoding="utf-8")
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.mode == "none"
    assert r.reason == "operator_paused"
    assert r.executed is False


@pytest.mark.asyncio
async def test_kill_switch_blocks(orchestrator: TradingOrchestrator, state_file: Path):
    state_file.write_text('{"paused":"false","kill_switch":"true","state":"TRADING","mode":"paper"}', encoding="utf-8")
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.mode == "none"
    assert r.reason == "kill_switch_active"
    assert r.executed is False


@pytest.mark.asyncio
async def test_risk_rejection_blocks(orchestrator: TradingOrchestrator):
    orchestrator.risk_manager.approve_trade = Mock(return_value=(False, "daily_loss_limit", 0.0))
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.executed is False
    assert "daily_loss_limit" in r.reason


@pytest.mark.asyncio
async def test_risk_position_sizing(orchestrator: TradingOrchestrator):
    orchestrator.risk_manager.approve_trade = Mock(return_value=(True, "approved", 123.0))
    await orchestrator.handle_opportunity(_base_opp())
    call = orchestrator.stealth.execute.await_args
    assert float(call.args[0]["approved_size_usd"]) == pytest.approx(123.0)


@pytest.mark.asyncio
async def test_stealth_execution_success(orchestrator: TradingOrchestrator):
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.executed is True
    assert r.tx_hash == "0xstealth"


@pytest.mark.asyncio
async def test_hunter_execution_success(orchestrator: TradingOrchestrator):
    opp = _base_opp()
    opp["force_hunter"] = True
    r = await orchestrator.handle_opportunity(opp)
    assert r.executed is True
    assert r.mode == "hunter"


@pytest.mark.asyncio
async def test_execution_failure_handling(orchestrator: TradingOrchestrator):
    orchestrator.stealth.execute.side_effect = RuntimeError("boom")
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.executed is False
    assert r.error is not None


@pytest.mark.asyncio
async def test_trade_recording(orchestrator: TradingOrchestrator):
    await orchestrator.handle_opportunity(_base_opp())
    orchestrator.trade_recorder.record_trade.assert_awaited_once()


@pytest.mark.asyncio
async def test_risk_state_update(orchestrator: TradingOrchestrator):
    await orchestrator.handle_opportunity(_base_opp())
    assert orchestrator.risk_manager.record_trade_result.call_count == 1


@pytest.mark.asyncio
async def test_latency_tracking(orchestrator: TradingOrchestrator):
    r = await orchestrator.handle_opportunity(_base_opp())
    assert r.latency_ms >= 0
    assert r.latency_ms < 5000


@pytest.mark.asyncio
async def test_metadata_preservation(orchestrator: TradingOrchestrator):
    r = await orchestrator.handle_opportunity(_base_opp())
    assert isinstance(r.metadata, dict)
    assert r.metadata.get("relay") == "test"


@pytest.mark.asyncio
async def test_concurrent_opportunities(orchestrator: TradingOrchestrator):
    async def _one(i: int):
        opp = _base_opp()
        opp["id"] = f"opp-{i}"
        return await orchestrator.handle_opportunity(opp)

    out = await asyncio.gather(*[_one(i) for i in range(10)])
    assert len(out) == 10
    assert all(x.executed for x in out)

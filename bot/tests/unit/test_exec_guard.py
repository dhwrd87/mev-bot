import pytest
from unittest.mock import AsyncMock

from bot.exec.guard import should_block_execution
from bot.exec.orderflow import Endpoint, PrivateOrderflowManager, TxMeta


def test_guard_disabled_does_not_block(monkeypatch):
    monkeypatch.setenv("FEATURE_EXEC_GUARD", "0")
    monkeypatch.setenv("FEATURE_EXEC_ENABLE", "0")
    blocked, reason = should_block_execution("unit")
    assert blocked is False
    assert reason == "guard_disabled"


def test_guard_blocks_when_feature_exec_disabled(monkeypatch):
    monkeypatch.setenv("FEATURE_EXEC_GUARD", "1")
    monkeypatch.setenv("FEATURE_EXEC_ENABLE", "0")
    monkeypatch.setenv("BOT_RUNTIME_STATE", "TRADING")
    blocked, reason = should_block_execution("unit")
    assert blocked is True
    assert reason == "feature_exec_enable_false"


def test_guard_blocks_when_state_not_allowed(monkeypatch):
    monkeypatch.setenv("FEATURE_EXEC_GUARD", "1")
    monkeypatch.setenv("FEATURE_EXEC_ENABLE", "1")
    monkeypatch.setenv("BOT_RUNTIME_STATE", "READY")
    monkeypatch.setenv("FEATURE_EXEC_ALLOWED_STATES", "TRADING")
    blocked, reason = should_block_execution("unit")
    assert blocked is True
    assert reason == "state_ready"


@pytest.mark.asyncio
async def test_private_orderflow_manager_skips_http_when_blocked(monkeypatch):
    monkeypatch.setenv("FEATURE_EXEC_GUARD", "1")
    monkeypatch.setenv("FEATURE_EXEC_ENABLE", "0")
    monkeypatch.setenv("BOT_RUNTIME_STATE", "TRADING")

    mgr = PrivateOrderflowManager([Endpoint(name="A", url="https://a", kind="rpc")])
    mgr._client.post = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError):
        await mgr.submit_private_tx("0xsigned", TxMeta(chain="sepolia"), retries_per_endpoint=0)
    mgr._client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_orderflow_manager_blocked_by_state_when_not_trading(monkeypatch):
    monkeypatch.setenv("FEATURE_EXEC_GUARD", "0")
    monkeypatch.setenv("BOT_RUNTIME_STATE", "READY")

    mgr = PrivateOrderflowManager([Endpoint(name="A", url="https://a", kind="rpc")])
    mgr._client.post = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError):
        await mgr.submit_private_tx("0xsigned", TxMeta(chain="sepolia"), retries_per_endpoint=0)
    mgr._client.post.assert_not_awaited()

from types import SimpleNamespace
import asyncio

from fastapi import HTTPException

import bot.api.main as api_main
from bot.core.state import BotState, BotStateMachine, build_state_machine, parse_bot_state


def test_parse_bot_state_accepts_string_and_enum():
    assert parse_bot_state("ready") == BotState.READY
    assert parse_bot_state(BotState.PAUSED) == BotState.PAUSED


def test_state_machine_allows_common_transitions():
    sm = BotStateMachine(state=BotState.SYNCING, lockdown=False)
    rec = sm.transition(BotState.READY, actor="system", reason="sync_done")
    assert rec.from_state == BotState.SYNCING.value
    assert rec.to_state == BotState.READY.value
    assert rec.actor == "system"
    assert rec.reason == "sync_done"
    assert isinstance(rec.ts_ms, int)
    assert rec.ts_ms > 0
    assert sm.state == BotState.READY

    rec = sm.transition(BotState.TRADING, actor="manual", reason="resume")
    assert rec.from_state == BotState.READY.value
    assert rec.to_state == BotState.TRADING.value
    assert sm.state == BotState.TRADING

    rec = sm.pause(actor="manual", reason="ops_pause")
    assert rec.from_state == BotState.TRADING.value
    assert rec.to_state == BotState.PAUSED.value
    assert sm.state == BotState.PAUSED


def test_state_machine_blocks_invalid_transitions():
    sm = BotStateMachine(state=BotState.PAUSED, lockdown=False)
    try:
        sm.transition(BotState.BOOTING, reason="unit")
    except ValueError as e:
        assert "invalid state transition" in str(e)
    else:
        raise AssertionError("Expected invalid transition to raise ValueError")


def test_build_state_machine_reads_env(monkeypatch):
    monkeypatch.setenv("BOT_INITIAL_STATE", "degraded")
    monkeypatch.setenv("BOT_STATE_LOCKDOWN", "true")
    sm = build_state_machine()
    assert sm.state == BotState.DEGRADED
    assert sm.lockdown is True


def test_default_state_is_paused(monkeypatch):
    monkeypatch.delenv("BOT_INITIAL_STATE", raising=False)
    sm = build_state_machine()
    assert sm.state == BotState.PAUSED


def test_health_includes_state(monkeypatch):
    monkeypatch.setattr(
        api_main,
        "get_chain_config",
        lambda: SimpleNamespace(
            chain="sepolia",
            chain_id=11155111,
            rpc_http_selected="http://rpc.local",
            ws_endpoints_selected=["wss://ws.local"],
        ),
    )
    api_main.app.state.w3 = None
    api_main.app.state.paused = True
    api_main.app.state.bot_state_machine = BotStateMachine(state=BotState.DEGRADED)

    payload = api_main.health()
    assert payload["ok"] is True
    assert payload["state"] == BotState.DEGRADED.value


def test_set_state_endpoint_updates_runtime(monkeypatch):
    api_main.app.state.bot_state_machine = BotStateMachine(state=BotState.PAUSED, lockdown=False)
    api_main.app.state.paused = True
    monkeypatch.setattr(api_main, "_write_paused_flag", lambda _: None)

    out = api_main.set_state("SYNCING", actor="system", reason="unit")
    assert out["ok"] is True
    assert out["state"] == BotState.SYNCING.value
    assert out["paused"] is False


def test_set_state_endpoint_rejects_invalid_transition(monkeypatch):
    api_main.app.state.bot_state_machine = BotStateMachine(state=BotState.SYNCING, lockdown=False)
    monkeypatch.setattr(api_main, "_write_paused_flag", lambda _: None)
    try:
        api_main.set_state("BOOTING", actor="system", reason="unit")
    except HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("expected HTTPException for invalid transition")


def test_chain_target_change_detection_success(monkeypatch):
    calls = []
    api_main.app.state._last_chain_target = ""
    monkeypatch.setattr(api_main, "_get_chain_snapshot", lambda: {"chain": "sepolia"})

    async def _fake_reload(name):
        calls.append(("reload", name))
        return {"ok": True}

    def _fake_transition(target, *, actor, reason, force=False):
        calls.append(("transition", str(target), actor, reason, force))
        return {"ok": True}

    monkeypatch.setattr(api_main, "_reload_chain_runtime", _fake_reload)
    monkeypatch.setattr(api_main, "_transition_state", _fake_transition)
    monkeypatch.setattr(api_main, "validate_chain_selection", lambda _sel: {"endpoint": "x", "wallet": "w", "balance": 1})

    asyncio.run(api_main._maybe_apply_chain_target("EVM:sepolia"))
    assert api_main.app.state._last_chain_target == "EVM:sepolia"
    assert any(c[0] == "reload" and c[1] == "EVM:sepolia" for c in calls)
    reasons = [c[3] for c in calls if c[0] == "transition"]
    assert any("chain_switch_pause" in r for r in reasons)
    assert any("chain_switch_syncing" in r for r in reasons)
    assert any("chain_switch_ready" in r for r in reasons)


def test_chain_target_change_detection_failure_sets_degraded(monkeypatch):
    calls = []
    api_main.app.state._last_chain_target = ""
    monkeypatch.setattr(api_main, "_get_chain_snapshot", lambda: {"chain": "sepolia"})

    async def _fake_reload(_name):
        raise RuntimeError("reload_fail")

    def _fake_transition(target, *, actor, reason, force=False):
        calls.append((str(target), reason))
        return {"ok": True}

    monkeypatch.setattr(api_main, "_reload_chain_runtime", _fake_reload)
    monkeypatch.setattr(api_main, "_transition_state", _fake_transition)

    asyncio.run(api_main._maybe_apply_chain_target("EVM:mainnet"))
    assert api_main.app.state._last_chain_target == "EVM:ethereum"
    assert any("DEGRADED" in t and "chain_switch_failed" in r for t, r in calls)


def test_chain_ready_hold_keeps_ready_when_operator_paused(monkeypatch):
    api_main.app.state.bot_state_machine = BotStateMachine(state=BotState.READY, lockdown=False)
    api_main.app.state._chain_switch_ready_hold_target = "EVM:sepolia"

    state, reason = api_main._apply_chain_ready_hold(
        op_state={"state": "PAUSED", "kill_switch": False},
        suggested_state=BotState.PAUSED,
        suggested_reason="operator_not_trading",
    )
    assert state == BotState.READY
    assert reason == "chain_switch_ready_hold"


def test_chain_ready_hold_clears_on_resume(monkeypatch):
    api_main.app.state.bot_state_machine = BotStateMachine(state=BotState.READY, lockdown=False)
    api_main.app.state._chain_switch_ready_hold_target = "EVM:sepolia"

    state, reason = api_main._apply_chain_ready_hold(
        op_state={"state": "TRADING", "kill_switch": False},
        suggested_state=BotState.READY,
        suggested_reason="healthy",
    )
    assert state == BotState.READY
    assert reason == "healthy"
    assert str(getattr(api_main.app.state, "_chain_switch_ready_hold_target", "")) == ""

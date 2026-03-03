import asyncio

import bot.ops.discord_operator as op_mod
from bot.ops.discord_operator import OperatorBot, _sum_prom_metric


def test_sum_prom_metric_aggregates_labeled_series():
    text = """
# HELP something
mevbot_mempool_stream_group_lag{stream="a"} 2
mevbot_mempool_stream_group_lag{stream="b"} 3
other_metric 9
""".strip()
    assert _sum_prom_metric(text, "mevbot_mempool_stream_group_lag") == 5.0


def test_dangerous_action_detection(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_TOKEN", "x")
    b = OperatorBot()
    assert b._is_dangerous("mode", "live") is True
    assert b._is_dangerous("kill_switch", "off") is True
    assert b._is_dangerous("mode", "paper") is False


def test_confirmation_token_roundtrip(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_TOKEN", "x")
    monkeypatch.setenv("DISCORD_OPERATOR_CONFIRM_TTL_S", "120")
    b = OperatorBot()
    code = b._new_confirm(user_id=123, action="mode", value="live", reason="test")
    got = b._take_confirm(user_id=123, code=code)
    assert got is not None
    assert got.action == "mode"
    assert got.value == "live"
    assert b._take_confirm(user_id=123, code=code) is None


def test_chain_set_transitions_pause_sync_ready(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_TOKEN", "x")
    b = OperatorBot()
    calls = []
    writes = {}
    audits = []

    async def fake_api_post(path, params=None):
        calls.append((path, params or {}))
        return {"ok": True}

    async def fake_set_state(target, *, actor, reason, force=False):
        calls.append((f"/state/{target}", {"actor": actor, "reason": reason, "force": force}))
        return {"ok": True, "state": target}

    async def fake_audit(text):
        audits.append(text)

    monkeypatch.setattr(b, "_api_post", fake_api_post)
    monkeypatch.setattr(b, "_set_state", fake_set_state)
    monkeypatch.setattr(b, "_audit", fake_audit)
    monkeypatch.setattr(op_mod, "_write_ops_value", lambda k, v: writes.__setitem__(k, v))
    monkeypatch.setattr(
        op_mod,
        "validate_chain_selection",
        lambda sel: {"endpoint": "https://rpc.test", "wallet": "0xabc", "balance": 1, "height_end": 100},
    )

    out = asyncio.run(
        b._apply_action(
            actor="tester",
            action="chain_set",
            value="EVM:sepolia",
            reason="unit",
        )
    )

    assert "chain_switch_ready" in out
    assert writes["chain_selection"] == "EVM:sepolia"
    assert calls[0][0] == "/state/PAUSED"
    assert calls[1][0] == "/chain/select"
    assert calls[2][0] == "/state/SYNCING"
    assert calls[3][0] == "/state/READY"
    assert any("action=chain_set" in line and "result=ready" in line for line in audits)
    asyncio.run(b.http.aclose())

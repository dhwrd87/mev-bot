import asyncio

import ops.discord_operator as op


class _DummyTask:
    def __init__(self, done: bool):
        self._done = done

    def done(self):
        return self._done


def _set_env(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_TOKEN", "x")
    monkeypatch.setenv("DISCORD_OPERATOR_COMMAND_CHANNEL_ID", "1")
    monkeypatch.setenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", "3")


def test_validate_required_env_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_COMMAND_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", raising=False)
    try:
        op._validate_required_env()
    except SystemExit as e:
        assert "Missing required env vars:" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_ensure_refresh_task_no_overlap(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    created = {"n": 0}

    def _fake_create_task(coro):
        created["n"] += 1
        if hasattr(coro, "close"):
            coro.close()
        return _DummyTask(done=False)

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
    assert bot._ensure_refresh_task() is True
    assert bot._ensure_refresh_task() is False
    assert created["n"] == 1
    asyncio.run(bot.httpx.aclose())


def test_snapshot_stale_transition_audited(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    audits = []

    async def _fake_audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(bot, "_audit", _fake_audit)

    async def _run():
        await bot._maybe_audit_snapshot_staleness({"__snapshot_stale": "0"})
        await bot._maybe_audit_snapshot_staleness({"__snapshot_stale": "1", "__snapshot_age_s": "77"})
        await bot._maybe_audit_snapshot_staleness({"__snapshot_stale": "0"})

    asyncio.run(_run())
    assert len(audits) == 2
    assert audits[0]["result"].startswith("stale")
    assert audits[0]["ok"] is False
    assert audits[1]["result"] == "resumed"
    assert audits[1]["ok"] is True
    asyncio.run(bot.httpx.aclose())

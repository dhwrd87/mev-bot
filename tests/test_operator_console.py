import asyncio
from types import SimpleNamespace

import aiohttp
import ops.discord_operator as op
from ops.discord_embeds import build_audit_embed


def _set_env(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_TOKEN", "x")
    monkeypatch.setenv("DISCORD_COMMAND_CHANNEL_ID", "100")
    monkeypatch.setenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "200")
    monkeypatch.setenv("DISCORD_OPERATOR_STATUS_CHANNEL_ID", "300")
    monkeypatch.setenv("DISCORD_OPERATOR_ROLE_IDS", "500")
    monkeypatch.delenv("DISCORD_OPERATOR_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_ALLOWED_ROLE_IDS", raising=False)


class _ReplySink:
    def __init__(self):
        self.calls = []

    async def __call__(self, msg):
        self.calls.append(msg)


def test_check_cmd_channel_denied_is_audited(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    sink = _ReplySink()
    audits = []

    async def _fake_audit(**kwargs):
        audits.append(kwargs)

    bot._audit = _fake_audit  # type: ignore[assignment]

    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=999),
        author=SimpleNamespace(id=42, display_name="alice", roles=[]),
        message=SimpleNamespace(content="!status"),
        command=SimpleNamespace(name="status"),
        reply=sink,
    )

    ok = asyncio.run(bot._check_cmd_channel(ctx))
    assert ok is False
    assert sink.calls
    assert audits and str(audits[0]["result"]).startswith("denied:wrong_channel")
    asyncio.run(bot.httpx.aclose())


def test_check_cmd_channel_allowed(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    sink = _ReplySink()
    audits = []

    async def _fake_audit(**kwargs):
        audits.append(kwargs)

    bot._audit = _fake_audit  # type: ignore[assignment]

    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=100),
        author=SimpleNamespace(id=42, display_name="alice", roles=[SimpleNamespace(id=500)]),
        message=SimpleNamespace(content="!status"),
        command=SimpleNamespace(name="status"),
        reply=sink,
    )

    ok = asyncio.run(bot._check_cmd_channel(ctx))
    assert ok is True
    assert not audits
    asyncio.run(bot.httpx.aclose())


def test_interaction_auth_denied_by_role(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    audits = []

    async def _fake_audit(**kwargs):
        audits.append(kwargs)

    sent = []

    async def _fake_resp(*args, **kwargs):
        sent.append((args, kwargs))

    bot._audit = _fake_audit  # type: ignore[assignment]
    bot._respond_interaction = _fake_resp  # type: ignore[assignment]

    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=100),
        user=SimpleNamespace(id=7, display_name="bob", roles=[SimpleNamespace(id=501)]),
    )

    ok = asyncio.run(bot._authorize_interaction(interaction, command="/status"))
    assert ok is False
    assert sent
    assert audits and audits[0]["result"].startswith("denied:")
    asyncio.run(bot.httpx.aclose())


def test_audit_embed_format_fields():
    em = build_audit_embed(
        {
            "ts_utc": "2026-03-03T00:00:00+00:00",
            "actor": "1:alice",
            "command": "!db",
            "result": "success",
            "ok": True,
        }
    )
    fields = {f.name: f.value for f in em.fields}
    assert em.title == "Operator Audit"
    assert fields["Actor"] == "1:alice"
    assert fields["Command"] == "!db"
    assert fields["Result"] == "success"


def test_operator_console_commands_registered(monkeypatch):
    _set_env(monkeypatch)
    bot = op.build_bot()
    try:
        for name in ("last", "top", "risk", "db", "status", "ping"):
            assert bot.get_command(name) is not None
    finally:
        asyncio.run(bot.httpx.aclose())


class _FakeResp:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, *, response: _FakeResp | None = None, err: Exception | None = None):
        self.response = response
        self.err = err
        self.closed = False
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if self.err:
            raise self.err
        return self.response

    async def close(self):
        self.closed = True


def test_api_get_builds_url_and_params(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("MEVBOT_API_URL", "http://api:8000")
    bot = op.OperatorBot()
    fake = _FakeSession(response=_FakeResp(status=200, text='{"ok":true,"items":[]}'))

    async def _run():
        bot._api_session = fake  # type: ignore[assignment]
        out = await bot._api_get("/attempts", params={"limit": 10})
        assert out.get("ok") is True
        assert fake.calls == [("http://api:8000/attempts", {"limit": 10})]

    asyncio.run(_run())
    asyncio.run(bot.httpx.aclose())


def test_api_get_handles_client_error(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("MEVBOT_API_URL", "http://api:8000")
    bot = op.OperatorBot()
    fake = _FakeSession(err=aiohttp.ClientConnectionError("down"))

    async def _run():
        bot._api_session = fake  # type: ignore[assignment]
        try:
            await bot._api_get("/attempts", params={"limit": 5})
        except RuntimeError as e:
            assert "API unavailable" in str(e)
        else:
            raise AssertionError("expected RuntimeError")

    asyncio.run(_run())
    asyncio.run(bot.httpx.aclose())


def test_fetch_attempts_falls_back_to_db_when_api_fails(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()

    async def _api_fail(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(bot, "_api_get", _api_fail)
    monkeypatch.setattr(
        op,
        "_db_fetchall",
        lambda q, p=(): [
            (
                None,
                "att1",
                "opp1",
                "default",
                "BLOCKED",
                "operator_not_trading",
                None,
                None,
                None,
                None,
                None,
                "evm",
                "sepolia",
                "testnet",
                {},
            )
        ],
    )

    async def _run():
        items, source = await bot._fetch_attempts(limit=5)
        assert source == "db"
        assert len(items) == 1
        assert items[0]["attempt_id"] == "att1"
        assert items[0]["status"] == "BLOCKED"

    asyncio.run(_run())
    asyncio.run(bot.httpx.aclose())


def test_handle_last_calls_api_get_without_attribute_errors(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()

    async def _api_ok(path, params=None):
        assert path == "/attempts"
        assert isinstance(params, dict) and int(params.get("limit", 0)) == 3
        return {
            "items": [
                {
                    "ts": "2026-03-03T12:00:00+00:00",
                    "strategy": "default",
                    "status": "BLOCKED",
                    "reason_code": "operator_not_trading",
                    "chain": "sepolia",
                    "meta": {},
                }
            ]
        }

    monkeypatch.setattr(bot, "_api_get", _api_ok)

    async def _run():
        out = await bot.handle_last(limit=3)
        assert int(out.get("rows", 0)) == 1
        assert str(out.get("source")) == "api"

    asyncio.run(_run())
    asyncio.run(bot.httpx.aclose())


def test_render_attempt_embeds_compact_story(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    items = [
        {
            "ts": "2026-03-03T12:00:00+00:00",
            "strategy": "flashloan_arb",
            "status": "BLOCKED",
            "reason_code": "sim_failed",
            "expected_pnl_usd": 12.5,
            "gas_estimate": 220000,
            "sim_outcome": "FAIL",
            "sim_revert_reason": "execution reverted: STALE_PRICE",
            "attempt_id": "att-123",
            "payload_hash": "0x" + "12" * 32,
            "tx_hash": "0x" + "ab" * 32,
            "chain": "sepolia",
            "meta": {},
        }
    ]
    embeds = bot._render_attempt_embeds(items, limit=10, title="Last 10 Attempts")
    assert embeds
    desc = embeds[0].description or ""
    assert "[2026-03-03 12:00:00Z attempt_ts]" in desc
    assert "flashloan_arb • BLOCKED • sim_failed" in desc
    assert "pnl=$12.5 gas=220000 sim=FAIL" in desc
    assert "attempt_id=att-123" in desc
    assert "payload=0x1212121212121212" in desc
    assert "revert=execution reverted: STALE_PRICE" in desc
    assert "sepolia.etherscan.io/tx/" not in desc
    asyncio.run(bot.httpx.aclose())


def test_render_attempt_embeds_uses_broadcasted_time_for_sent(monkeypatch):
    _set_env(monkeypatch)
    bot = op.OperatorBot()
    items = [
        {
            "ts": "2026-03-03T15:05:00+00:00",
            "broadcasted_at": "2026-03-03T14:00:00+00:00",
            "strategy": "dex_arb",
            "status": "SENT",
            "reason_code": "none",
            "expected_pnl_usd": 3.1,
            "gas_estimate": 110000,
            "sim_outcome": "PASS",
            "tx_hash": "0x" + "ab" * 32,
            "chain": "sepolia",
            "explorer_base": "https://sepolia.etherscan.io",
            "meta": {},
        }
    ]
    embeds = bot._render_attempt_embeds(items, limit=10, title="Last 10 Attempts")
    desc = embeds[0].description or ""
    assert "[2026-03-03 14:00:00Z broadcasted_at]" in desc
    assert "tx=[0xabababab…](https://sepolia.etherscan.io/tx/" in desc
    asyncio.run(bot.httpx.aclose())

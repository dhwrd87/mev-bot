from fastapi import HTTPException
import asyncio

import bot.api.main as api_main


def test_operator_mode_prepare_and_commit_live_token_flow(monkeypatch):
    saved = {}

    def _fake_write(patch, actor):
        saved["patch"] = patch
        saved["actor"] = actor
        return {"ok": True}

    monkeypatch.setattr(api_main, "_write_operator_state_patch", _fake_write)
    api_main._MODE_CONFIRM_TOKENS.clear()

    prep = api_main.operator_mode_prepare(api_main.OperatorModePrepareBody(mode="live", actor="u1"))
    assert prep["ok"] is True
    assert prep["requires_confirm"] is True
    token = prep["token"]

    commit = api_main.operator_mode_commit(api_main.OperatorModeCommitBody(mode="live", actor="u1", token=token))
    assert commit["ok"] is True
    assert saved["patch"]["mode"] == "live"
    assert saved["actor"] == "u1"


def test_operator_mode_live_requires_token():
    try:
        api_main.operator_mode(api_main.OperatorModeBody(mode="live", actor="u2"))
    except HTTPException as e:
        assert e.status_code == 409
        assert "requires confirmation token" in str(e.detail)
    else:
        raise AssertionError("expected HTTPException for missing live token")


def test_operator_mode_commit_rejects_wrong_actor():
    api_main._MODE_CONFIRM_TOKENS.clear()
    prep = api_main.operator_mode_prepare(api_main.OperatorModePrepareBody(mode="live", actor="owner"))
    token = prep["token"]
    try:
        api_main.operator_mode_commit(api_main.OperatorModeCommitBody(mode="live", actor="other", token=token))
    except HTTPException as e:
        assert e.status_code == 400
        assert "actor_mismatch" in str(e.detail)
    else:
        raise AssertionError("expected actor mismatch failure")


def test_status_contract_fields(monkeypatch):
    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _R:
                def fetchone(self_non):
                    # attempts, blocked, sims, sim_fail, accept, reject, sent
                    return (11, 3, 10, 2, 4, 6, 5)

            return _R()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(api_main, "_db_connect", lambda: _Conn())
    monkeypatch.setattr(api_main, "_get_chain_snapshot", lambda: {"chain": "sepolia", "chain_id": 11155111})
    monkeypatch.setattr(
        api_main,
        "get_operator_state",
        lambda: {"mode": "paper", "kill_switch": False, "state": "PAUSED", "last_actor": "u", "chain_target": "EVM:sepolia"},
    )
    api_main.app.state.bot_state_machine = None
    api_main.app.state.paused = True
    api_main.app.state.started_ts = 0.0
    api_main.app.state._chain_obs_last_head = 123
    api_main.app.state._chain_obs_max_head = 130

    out = api_main.status()
    for key in (
        "bot_state",
        "mode",
        "chain",
        "paused",
        "killswitch",
        "head",
        "lag",
        "uptime",
        "attempts_10m",
        "blocked_10m",
        "sim_fail_rate_10m",
        "trades_sent_10m",
        "desired_chain",
        "effective_chain",
        "resolved_chain_id",
        "rpc_url",
        "config_validation_ok",
        "config_validation_reasons",
        "operator_restart_required",
        "restart_required_reason",
        "worker_build_id",
        "worker_uptime_s",
    ):
        assert key in out
    assert out["mode"] == "paper"
    assert out["chain"] == "sepolia"
    assert out["attempts_10m"] == 11


def test_operator_chain_sets_desired_and_hot_applies(monkeypatch):
    saved = {}

    def _fake_write(patch, actor):
        saved["patch"] = dict(patch)
        saved["actor"] = actor
        return dict(patch)

    monkeypatch.setattr(api_main, "_write_operator_state_patch", _fake_write)
    async def _fake_maybe_apply(chain_target, desired_state="PAUSED"):
        return None
    monkeypatch.setattr(api_main, "_maybe_apply_chain_target", _fake_maybe_apply)
    monkeypatch.setattr(
        api_main,
        "status",
        lambda: {
            "effective_chain": "EVM:sepolia",
            "desired_chain": "EVM:bnb-testnet",
            "switching_in_progress": False,
            "last_transition_error": "",
            "operator_restart_required": False,
        },
    )

    out = asyncio.run(api_main.operator_chain(api_main.OperatorChainBody(chain="EVM:bnb-testnet", actor="u1")))
    assert out["ok"] is True
    assert out["desired_chain"] == "EVM:bnb-testnet"
    assert out["needs_restart"] is False
    assert saved["patch"]["chain_target"] == "EVM:bnb-testnet"


def test_operator_chain_rejects_unsupported_family_without_state_write(monkeypatch):
    monkeypatch.setenv("SUPPORTED_FAMILIES", "EVM")
    called = {"n": 0}

    def _fake_write(_patch, _actor):
        called["n"] += 1
        return {}

    monkeypatch.setattr(api_main, "_write_operator_state_patch", _fake_write)
    try:
        asyncio.run(api_main.operator_chain(api_main.OperatorChainBody(chain="SOL:solana", actor="u1")))
    except HTTPException as e:
        assert e.status_code == 400
        assert "SUPPORTED_FAMILIES=EVM" in str(e.detail)
    else:
        raise AssertionError("expected unsupported family error")
    assert called["n"] == 0


def test_attempts_masks_tx_hash_when_not_sent(monkeypatch):
    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _R:
                def fetchall(self_non):
                    from datetime import datetime, timezone

                    now = datetime.now(timezone.utc)
                    return [
                        (
                            now,  # ts
                            "att1",
                            "opp1",
                            1000,
                            "default",
                            "BLOCKED",
                            "operator_not_trading",
                            1.23,
                            21000.0,
                            "FAIL",
                            "execution_reverted",
                            "FAIL: execution_reverted",
                            "0x" + "11" * 32,  # payload_hash
                            "0x" + "ab" * 32,  # tx_hash (must be masked for BLOCKED)
                            None,
                            None,
                            None,
                            "evm",
                            "sepolia",
                            "testnet",
                            "paper",
                            {},
                            now,
                            now,
                        ),
                        (
                            now,  # ts
                            "att2",
                            "opp2",
                            2000,
                            "default",
                            "SENT",
                            "none",
                            2.0,
                            30000.0,
                            "PASS",
                            None,
                            "OK",
                            "0x" + "22" * 32,  # payload_hash
                            "0x" + "cd" * 32,  # tx_hash visible for SENT
                            now,
                            None,
                            None,
                            "evm",
                            "sepolia",
                            "testnet",
                            "live",
                            {},
                            now,
                            now,
                        ),
                    ]

            return _R()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(api_main, "_db_connect", lambda: _Conn())
    monkeypatch.setattr(api_main, "_attempt_explorer_base", lambda **_: "https://sepolia.etherscan.io")

    out = api_main.list_attempts(limit=10)
    assert out["ok"] is True
    assert len(out["items"]) == 2
    blocked = out["items"][0]
    sent = out["items"][1]

    assert blocked["status"] == "BLOCKED"
    assert blocked["payload_hash"]
    assert blocked["tx_hash"] is None
    assert blocked["explorer_base"] == "https://sepolia.etherscan.io"

    assert sent["status"] == "SENT"
    assert sent["tx_hash"] == "0x" + "cd" * 32
    assert sent["broadcasted_at"] is not None


def test_runtime_readiness_report_has_meaningful_reasons(monkeypatch):
    monkeypatch.setattr(
        api_main,
        "_get_chain_snapshot",
        lambda: {
            "rpc_http_selected": "",
            "chain": "sepolia",
            "family": "evm",
            "chain_id": 11155111,
        },
    )
    api_main.app.state._chain_obs_last_head = 0
    api_main.app.state._chain_obs_last_slot = 0
    api_main.app.state._chain_obs_last_advance_ts = 0.0
    rep = api_main._runtime_readiness_report()
    assert rep["overall_ok"] is False
    assert "rpc_url_non_null:rpc_url_missing" in rep["failed"]
    assert any(str(x).startswith("head_advancing:") for x in rep["failed"])


def test_readiness_endpoint_returns_failed_reason_list(monkeypatch):
    monkeypatch.setattr(
        api_main,
        "_runtime_readiness_report",
        lambda: {
            "overall_ok": False,
            "checks": [
                {"name": "rpc_url_non_null", "ok": False, "required": True, "error": "rpc_url_missing", "details": {}}
            ],
            "failed": ["rpc_url_non_null:rpc_url_missing"],
        },
    )
    monkeypatch.setattr(api_main.Path, "exists", lambda self: False, raising=False)
    out = api_main.readiness()
    assert out["ok"] is False
    assert out["failed"] == ["rpc_url_non_null:rpc_url_missing"]


def test_normalize_reason_code_mappings():
    assert api_main._normalize_reason_code("none_selected") == "no_routes"
    assert api_main._normalize_reason_code("detector_miss") == "no_routes"
    assert api_main._normalize_reason_code("low_edge_bps") == "min_profit"
    assert api_main._normalize_reason_code("high_gas_gwei") == "gas_guard"
    assert api_main._normalize_reason_code("decoder_error") == "decoder_fail"


def test_strategies_endpoint_returns_enabled_and_counters(monkeypatch):
    monkeypatch.setattr(api_main, "_configured_strategy_names", lambda: ["candidate_pipeline", "hunter"])
    monkeypatch.setattr(
        api_main,
        "get_operator_state",
        lambda: {"strategy_overrides": {"allowlist": ["candidate_pipeline"], "denylist": []}},
    )

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _R:
                def fetchall(self_non):
                    return [
                        ("candidate_pipeline", 10, 4, 3, 1, "none_selected", 2),
                        ("candidate_pipeline", 10, 4, 3, 1, "high_gas_gwei", 1),
                    ]

            return _R()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(api_main, "_db_connect", lambda: _Conn())
    out = api_main.strategies(window="10m")
    assert out["ok"] is True
    assert out["enabled"] == ["candidate_pipeline"]
    rows = {r["strategy"]: r for r in out["items"]}
    assert rows["candidate_pipeline"]["seen"] == 10
    assert rows["candidate_pipeline"]["reject_reasons"]["no_routes"] == 2
    assert rows["candidate_pipeline"]["reject_reasons"]["gas_guard"] == 1


def test_pipeline_endpoint_returns_diagnostics(monkeypatch):
    monkeypatch.setattr(api_main, "_detectors_from_config", lambda: ["sniper", "sandwich_victim"])
    monkeypatch.setattr(
        api_main,
        "_token_universe_status",
        lambda: {"path": "config/token_universe.json", "loaded": True, "count": 2, "error": None},
    )
    api_main.app.state._dex_enabled = ["univ2_default"]
    api_main.app.state.last_transition_error = ""

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _R:
                def fetchall(self_non):
                    from datetime import datetime, timezone

                    now = datetime.now(timezone.utc)
                    return [
                        (now, "simulation", "sim_failed", "revert: out_of_gas"),
                        (now, "attempt", "decoder_error", "bad calldata"),
                    ]

            return _R()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(api_main, "_db_connect", lambda: _Conn())
    out = api_main.pipeline(limit=5)
    assert out["ok"] is True
    assert out["detectors_active"] == ["sniper", "sandwich_victim"]
    assert out["token_universe"]["loaded"] is True
    assert out["dex_packs"]["loaded"] is True
    assert out["last_sim_error"] is not None

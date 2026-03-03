import json
from concurrent.futures import ThreadPoolExecutor

from ops.operator_state import (
    default_state,
    load_state,
    save_state,
    update_state,
    validate_state,
)


def test_validate_state_schema_ok():
    out = validate_state(
        {
            "state": "paused",
            "mode": "paper",
            "kill_switch": True,
            "chain_target": "EVM:base",
            "risk_overrides": {"allow_tokens": ["0xabc"], "deny_pools": ["0xpool"]},
            "enabled_dex_overrides": {"allowlist": ["univ3"], "denylist": ["jupiter"]},
            "last_updated": "2026-01-01T00:00:00+00:00",
            "last_actor": "123:alice",
        }
    )
    assert out["state"] == "PAUSED"
    assert out["mode"] == "paper"
    assert out["kill_switch"] is True
    assert out["chain_target"] == "EVM:base"
    assert out["risk_overrides"]["allow_tokens"] == ["0xabc"]
    assert out["risk_overrides"]["deny_pools"] == ["0xpool"]
    assert out["enabled_dex_overrides"]["allowlist"] == ["univ3"]
    assert out["enabled_dex_overrides"]["denylist"] == ["jupiter"]


def test_validate_state_canonicalizes_chain_target_alias():
    out = validate_state({"state": "READY", "mode": "paper", "chain_target": "ethereum"})
    assert out["chain_target"] == "EVM:ethereum"


def test_validate_state_rejects_invalid_values():
    try:
        validate_state({"state": "BROKEN", "mode": "paper"})
    except ValueError as e:
        assert "invalid state" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_load_save_atomic_roundtrip(tmp_path):
    p = tmp_path / "operator_state.json"
    seed = default_state()
    saved = save_state(p, seed, actor="1:test")
    loaded = load_state(p)
    assert loaded["state"] == saved["state"]
    assert loaded["last_actor"] == "1:test"

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw["last_actor"] == "1:test"


def test_concurrent_updates_do_not_corrupt_json(tmp_path):
    p = tmp_path / "operator_state.json"
    save_state(p, default_state(), actor="system")

    def _writer(i: int):
        mode = "paper" if i % 2 == 0 else "dryrun"
        chain = "base" if i % 2 == 0 else "sepolia"
        update_state(
            p,
            {
                "mode": mode,
                "kill_switch": bool(i % 3 == 0),
                "chain_target": chain,
            },
            actor=f"{i}:writer",
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_writer, range(100)))

    loaded = load_state(p)
    assert loaded["mode"] in {"paper", "dryrun", "live", "UNKNOWN"}
    assert "last_actor" in loaded
    assert loaded["chain_target"] in {"EVM:base", "EVM:sepolia"}
    # Must always be valid JSON after concurrent writes.
    json.loads(p.read_text(encoding="utf-8"))


def test_status_message_id_persists(tmp_path):
    p = tmp_path / "operator_state.json"
    save_state(p, default_state(), actor="system")
    update_state(p, {"status_message_id": 123456789}, actor="42:op")
    loaded = load_state(p)
    assert loaded["status_message_id"] == 123456789


def test_dex_override_fields_roundtrip(tmp_path):
    p = tmp_path / "operator_state.json"
    save_state(p, default_state(), actor="system")
    update_state(
        p,
        {"enabled_dex_overrides": {"allowlist": ["univ2_sushi"], "denylist": ["jupiter_main"]}},
        actor="42:op",
    )
    loaded = load_state(p)
    assert loaded["enabled_dex_overrides"]["allowlist"] == ["univ2_sushi"]
    assert loaded["enabled_dex_overrides"]["denylist"] == ["jupiter_main"]


def test_risk_override_fields_roundtrip(tmp_path):
    p = tmp_path / "operator_state.json"
    save_state(p, default_state(), actor="system")
    update_state(
        p,
        {"risk_overrides": {"allow_tokens": ["0xabc"], "deny_tokens": ["0xdef"], "watch_pools": ["0xpool"]}},
        actor="42:op",
    )
    loaded = load_state(p)
    assert loaded["risk_overrides"]["allow_tokens"] == ["0xabc"]
    assert loaded["risk_overrides"]["deny_tokens"] == ["0xdef"]
    assert loaded["risk_overrides"]["watch_pools"] == ["0xpool"]


def test_strategy_limits_flashloan_roundtrip(tmp_path):
    p = tmp_path / "operator_state.json"
    save_state(p, default_state(), actor="system")
    update_state(
        p,
        {
            "flashloan_enabled": True,
            "strategy_overrides": {"allowlist": ["opportunity_engine"], "denylist": ["hunter"]},
            "limits": {
                "max_fee_gwei": 120,
                "slippage_bps": 80,
                "max_daily_loss_usd": 250.5,
                "min_edge_bps": 9,
            },
        },
        actor="42:op",
    )
    loaded = load_state(p)
    assert loaded["flashloan_enabled"] is True
    assert loaded["strategy_overrides"]["allowlist"] == ["opportunity_engine"]
    assert loaded["strategy_overrides"]["denylist"] == ["hunter"]
    assert loaded["limits"]["max_fee_gwei"] == 120.0
    assert loaded["limits"]["slippage_bps"] == 80.0
    assert loaded["limits"]["max_daily_loss_usd"] == 250.5
    assert loaded["limits"]["min_edge_bps"] == 9.0

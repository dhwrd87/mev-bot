from __future__ import annotations

from ops.operator_state import default_state, save_state
from risk.risk_firewall import RiskFirewall


def _write_state(path, patch):
    st = default_state()
    st.update(patch)
    save_state(path, st, actor="test")


def test_risk_firewall_manual_overrides(tmp_path):
    p = tmp_path / "operator_state.json"
    _write_state(
        p,
        {
            "risk_overrides": {
                "allow_tokens": ["0xallow"],
                "deny_tokens": ["0xdeny"],
            }
        },
    )
    fw = RiskFirewall(chain="sepolia", operator_state_path=str(p))

    d_allow = fw.evaluate(token="0xALLOW", pool="pool-a")
    assert d_allow.classification == "ALLOW"
    assert "manual_override_allow" in d_allow.reasons

    d_deny = fw.evaluate(token="0xDENY", pool="pool-a")
    assert d_deny.classification == "DENY"
    assert "manual_override_deny" in d_deny.reasons


def test_risk_firewall_dynamic_sell_fail_denies(tmp_path):
    p = tmp_path / "operator_state.json"
    _write_state(p, {})
    fw = RiskFirewall(chain="sepolia", operator_state_path=str(p))

    d = fw.evaluate(
        token="0xtoken",
        pool="pool-x",
        simulate_buy=lambda: (True, "ok"),
        simulate_sell=lambda: (False, "reverted"),
    )
    assert d.classification == "DENY"
    assert any(r.startswith("sell_sim_failed:") for r in d.reasons)

    excluded, d2 = fw.should_exclude(
        token="0xtoken",
        pool="pool-x",
        simulate_buy=lambda: (True, "ok"),
        simulate_sell=lambda: (False, "reverted"),
    )
    assert excluded is True
    assert d2.classification == "DENY"


def test_risk_firewall_static_watch(tmp_path):
    p = tmp_path / "operator_state.json"
    _write_state(p, {})
    fw = RiskFirewall(chain="sepolia", operator_state_path=str(p))

    d = fw.evaluate(
        token="0xtoken",
        pool="pool-y",
        metadata={"is_proxy": True, "owner_renounced": False},
        simulate_buy=lambda: (True, "ok"),
        simulate_sell=lambda: (True, "ok"),
    )
    assert d.classification == "WATCH"
    assert "proxy_contract" in d.reasons
    assert "owner_not_renounced" in d.reasons

from bot.exec.orderflow import should_broadcast


def test_should_broadcast_paused():
    ok, reason = should_broadcast(
        {
            "scope": "submit_private_tx",
            "chain": "sepolia",
            "mode": "live",
            "runtime_state": "TRADING",
            "sim_ok": True,
            "operator_state": {"state": "PAUSED", "kill_switch": False, "mode": "live", "limits": {}},
        }
    )
    assert ok is False
    assert reason == "operator_not_trading"


def test_should_broadcast_kill_switch():
    ok, reason = should_broadcast(
        {
            "scope": "submit_private_tx",
            "chain": "sepolia",
            "mode": "live",
            "runtime_state": "TRADING",
            "sim_ok": True,
            "operator_state": {"state": "TRADING", "kill_switch": True, "mode": "live", "limits": {}},
        }
    )
    assert ok is False
    assert reason == "operator_kill_switch"


def test_should_broadcast_sim_fail():
    ok, reason = should_broadcast(
        {
            "scope": "submit_private_tx",
            "chain": "sepolia",
            "mode": "live",
            "runtime_state": "TRADING",
            "sim_ok": False,
            "operator_state": {"state": "TRADING", "kill_switch": False, "mode": "live", "limits": {}},
        }
    )
    assert ok is False
    assert reason == "sim_failed"


def test_should_broadcast_risk_fail():
    ok, reason = should_broadcast(
        {
            "scope": "submit_private_tx",
            "chain": "sepolia",
            "mode": "live",
            "runtime_state": "TRADING",
            "sim_ok": True,
            "fee_gwei": 200,
            "operator_state": {
                "state": "TRADING",
                "kill_switch": False,
                "mode": "live",
                "limits": {"max_fee_gwei": 10},
            },
        }
    )
    assert ok is False
    assert reason == "risk_max_fee_gwei"


def test_should_broadcast_happy_path_dryrun():
    ok, reason = should_broadcast(
        {
            "scope": "submit_private_tx",
            "chain": "sepolia",
            "mode": "dryrun",
            "runtime_state": "TRADING",
            "operator_state": {"state": "TRADING", "kill_switch": False, "mode": "dryrun", "limits": {}},
        }
    )
    assert ok is True
    assert reason == "allowed"

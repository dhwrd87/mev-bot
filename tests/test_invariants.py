from bot.core.invariants import RuntimeInvariants
from bot.core.state import BotState


def _inv() -> RuntimeInvariants:
    return RuntimeInvariants(
        rpc_p99_ms_threshold=100.0,
        rpc_window_min=5,
        drawdown_threshold=0.2,
        tx_fail_rate_threshold=0.5,
        tx_fail_window_min=5,
        _rpc_samples=__import__("collections").deque(maxlen=1000),
        _tx_samples=__import__("collections").deque(maxlen=1000),
    )


def test_operator_kill_switch_forces_panic():
    inv = _inv()
    s, reason = inv.evaluate(operator_state={"kill_switch": True, "state": "TRADING"}, drawdown=0.0, now=100.0)
    assert s == BotState.PANIC
    assert reason == "operator_kill_switch"


def test_operator_not_trading_forces_paused():
    inv = _inv()
    s, reason = inv.evaluate(operator_state={"kill_switch": False, "state": "PAUSED"}, drawdown=0.0, now=100.0)
    assert s == BotState.PAUSED
    assert reason == "operator_not_trading"


def test_drawdown_forces_panic():
    inv = _inv()
    s, reason = inv.evaluate(operator_state={"kill_switch": False, "state": "TRADING"}, drawdown=0.25, now=100.0)
    assert s == BotState.PANIC
    assert reason == "drawdown_limit"


def test_rpc_p99_high_degrades():
    inv = _inv()
    for i in range(100):
        inv.observe_rpc_latency_ms(200.0 if i > 95 else 10.0, now=100.0 + i)
    s, reason = inv.evaluate(operator_state={"kill_switch": False, "state": "TRADING"}, drawdown=0.0, now=300.0)
    assert s == BotState.DEGRADED
    assert reason == "rpc_p99_high"


def test_tx_failure_rate_high_degrades():
    inv = _inv()
    for i in range(10):
        inv.observe_tx_result(ok=(i < 3), now=100.0 + i)  # 70% fail
    s, reason = inv.evaluate(operator_state={"kill_switch": False, "state": "TRADING"}, drawdown=0.0, now=200.0)
    assert s == BotState.DEGRADED
    assert reason == "tx_failure_rate_high"


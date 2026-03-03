from ops.confirmations import ConfirmationStore


def test_confirmation_actor_binding():
    s = ConfirmationStore(ttl_s=60.0)
    c = s.create(action="mode", args={"value": "live"}, actor_id=42, now=100.0)

    ok_bad, _, reason_bad = s.consume(token=c.token, actor_id=99, now=101.0)
    assert ok_bad is False
    assert reason_bad == "actor_mismatch"

    ok_good, item, reason_good = s.consume(token=c.token, actor_id=42, now=101.0)
    assert ok_good is True
    assert reason_good == "ok"
    assert item is not None
    assert item.action == "mode"


def test_confirmation_expiry():
    s = ConfirmationStore(ttl_s=60.0)
    c = s.create(action="kill", args={"value": "off"}, actor_id=7, now=100.0)
    ok, _, reason = s.consume(token=c.token, actor_id=7, now=161.0)
    assert ok is False
    assert reason == "invalid_or_expired" or reason == "expired"


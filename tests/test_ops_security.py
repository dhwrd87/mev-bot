from ops.security import CommandGuard, UserRateLimiter, parse_id_csv


def test_parse_id_csv():
    assert parse_id_csv("") == set()
    assert parse_id_csv(None) == set()
    assert parse_id_csv("1, 2,3") == {1, 2, 3}


def test_guard_user_allowlist_precedence():
    g = CommandGuard(allowed_user_ids={42}, allowed_role_ids={7})
    ok, reason = g.authorize(user_id=42, role_ids=[1])
    assert ok is True
    assert reason == "ok_user"

    ok, reason = g.authorize(user_id=9, role_ids=[7])
    assert ok is False
    assert reason == "unauthorized_user"


def test_guard_role_allowlist():
    g = CommandGuard(allowed_user_ids=set(), allowed_role_ids={7, 8})
    ok, reason = g.authorize(user_id=9, role_ids=[1, 8])
    assert ok is True
    assert reason == "ok_role"

    ok, reason = g.authorize(user_id=9, role_ids=[1, 2])
    assert ok is False
    assert reason == "unauthorized_role"


def test_rate_limiter_window():
    rl = UserRateLimiter(limit=3, window_s=10.0)
    assert rl.allow(1, now=100.0)[0] is True
    assert rl.allow(1, now=101.0)[0] is True
    assert rl.allow(1, now=102.0)[0] is True
    ok, retry = rl.allow(1, now=103.0)
    assert ok is False
    assert 0.0 < retry <= 10.0

    # After window passes, command should be allowed again.
    ok2, retry2 = rl.allow(1, now=111.1)
    assert ok2 is True
    assert retry2 == 0.0


from bot.quote import v2_math


def test_v2_math_contract():
    amt_out = v2_math.get_amount_out(amount_in=1_000, reserve_in=1_000_000, reserve_out=500_000, fee_bps=30)
    amt_in = v2_math.get_amount_in(amount_out=100, reserve_in=1_000_000, reserve_out=500_000, fee_bps=30)

    assert isinstance(amt_out, int)
    assert isinstance(amt_in, int)
    assert amt_out > 0
    assert amt_in > 0

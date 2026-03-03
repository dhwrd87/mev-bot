from bot.quote.sizer import SizingCaps, OptimalTradeSizer


def test_sizer_contract():
    caps = SizingCaps(max_in_abs=1_000_000, max_out_abs=500_000)
    sizer = OptimalTradeSizer(caps)

    want_out, max_in = sizer.size_exact_out(
        reserve_in=5_000_000,
        reserve_out=2_000_000,
        fee_bps=30,
        impact_bps=200,
    )

    assert isinstance(want_out, int)
    assert isinstance(max_in, int)
    assert want_out > 0
    assert max_in > 0

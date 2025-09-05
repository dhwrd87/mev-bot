import pytest
from bot.quote.sizer import OptimalTradeSizer, SizingCaps

def test_sizer_respects_caps_and_scales():
    caps = SizingCaps(
        max_in_abs=1_000_000_000_000_000_000,   # 1e18
        max_out_abs=10_000_000_000_000_000_000, # 1e19
        max_pool_pct=0.01, safety_overpay=0.05, impact_fraction_to_capture=0.3
    )
    sizer = OptimalTradeSizer(caps)
    r_in, r_out, fee, impact = 10**21, 10**21, 30, 300  # 3% impact
    want, max_in = sizer.size_exact_out(r_in, r_out, fee, impact)
    assert want > 0 and max_in > 0
    # raise impact -> want should (weakly) increase
    want2, _ = sizer.size_exact_out(r_in, r_out, fee, 600)
    assert want2 >= want

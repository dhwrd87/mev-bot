import pytest
from bot.quote.v2_math import get_amount_out, get_amount_in

def test_v2_inverse_roundtrip():
    r_in, r_out, fee = 1_000_000_000_000, 2_000_000_000_000, 30
    for x in [10, 10_000, 10**9]:
        out = get_amount_out(x, r_in, r_out, fee)
        need = get_amount_in(out, r_in, r_out, fee)
        assert need >= x  # since +1 rounding in inverse

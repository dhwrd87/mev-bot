# bot/quote/v2_math.py
from __future__ import annotations

def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """
    Uniswap V2 formula with fee taken from input: amountOut = (amountIn*(1-fee)*reserveOut)/(reserveIn+(amountIn*(1-fee)))
    fee_bps: 30 -> 0.3% typical
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    num = amount_in_with_fee * reserve_out
    den = reserve_in * 10_000 + amount_in_with_fee
    return num // den

def get_amount_in(amount_out: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """
    Inverse of get_amount_out: amountIn = (reserveIn*amountOut*10_000)/( (reserveOut-amountOut)*(10_000-fee) ) + 1
    """
    if amount_out <= 0 or reserve_in <= 0 or reserve_out <= 0 or amount_out >= reserve_out:
        return 0
    num = reserve_in * amount_out * 10_000
    den = (reserve_out - amount_out) * (10_000 - fee_bps)
    return num // den + 1

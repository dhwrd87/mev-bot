# tests/unit/test_quoter_guard.py
import pytest
from unittest.mock import MagicMock
from bot.exec.exact_output import ExactOutputSwapper

def mk_contract(ret_pool="0x0000000000000000000000000000000000000000"):
    c = MagicMock()
    c.functions.getPool.return_value.call.return_value = ret_pool
    return c

def mk_quoter(amount_in=12345):
    q = MagicMock()
    q.functions.quoteExactOutputSingle.return_value.call.return_value = amount_in
    return q

def test_no_pool_returns_none(monkeypatch):
    swapper = ExactOutputSwapper.__new__(ExactOutputSwapper)
    swapper.factory = mk_contract("0x0")
    swapper.quoter = mk_quoter()
    assert swapper.safe_quote_exact_output("0x1","0x2",1000,0) is None

def test_pool_quotes_amount(monkeypatch):
    swapper = ExactOutputSwapper.__new__(ExactOutputSwapper)
    swapper.factory = mk_contract("0x000000000000000000000000000000000000dEaD")
    swapper.quoter = mk_quoter(1000)
    out = swapper.safe_quote_exact_output("0x1","0x2",500,0)
    assert out and out["fee"] in (500,3000,10000) and out["amountInMaximum"] >= 1000

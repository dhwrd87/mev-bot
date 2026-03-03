from __future__ import annotations

import json
import time

from ops.operator_state import default_state, save_state
from bot.core.opportunity_engine.types import MarketEvent
from bot.core.types_dex import Quote
from bot.detectors.triarb_detector import TriArbDetector


class _Sel:
    def __init__(self, dex: str, out: int, fee: float = 0.0):
        self.dex = dex
        self.quote = Quote(
            dex=dex,
            expected_out=int(out),
            min_out=max(1, int(out) - 1),
            price_impact_bps=1.0,
            fee_estimate=float(fee),
            route_summary="x",
            quote_latency_ms=1.0,
        )
        self.quote_table = []

    @property
    def candidates(self):
        return self.quote_table


class _FakeRouter:
    def __init__(self):
        self.registry = type("R", (), {"enabled_names": lambda self: ["dex_a", "dex_b"]})()

    def route(self, intent):
        a, b = intent.token_in, intent.token_out
        amt = int(intent.amount_in)
        # A -> B -> C -> A profitable by construction.
        if (a, b) == ("A", "B"):
            return _Sel("dex_a", int(amt * 1.05), fee=0.1)
        if (a, b) == ("B", "C"):
            return _Sel("dex_b", int(amt * 1.05), fee=0.1)
        if (a, b) == ("C", "A"):
            return _Sel("dex_a", int(amt * 0.96), fee=0.1)
        return None


def _event() -> MarketEvent:
    return MarketEvent(
        id="e1",
        ts=time.time(),
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="A",
        token_out="B",
        amount_hint=100,
        source="test",
    )


def test_triarb_detector_emits_opportunity(tmp_path, monkeypatch):
    u = tmp_path / "token_universe.json"
    u.write_text(
        json.dumps(
            [
                {"family": "evm", "chain": "sepolia", "network": "testnet", "token_in": "A", "token_out": "B", "sizes": [100], "liquidity_usd": 10000},
                {"family": "evm", "chain": "sepolia", "network": "testnet", "token_in": "B", "token_out": "C", "sizes": [100], "liquidity_usd": 10000},
                {"family": "evm", "chain": "sepolia", "network": "testnet", "token_in": "C", "token_out": "A", "sizes": [100], "liquidity_usd": 10000},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRIARB_MIN_EDGE_BPS", "1.0")
    d = TriArbDetector(_FakeRouter(), universe_path=str(u))
    out = d.on_event(_event())
    assert len(out) == 1
    opp = out[0]
    assert opp.type == "triarb"
    assert opp.constraints["path_tokens"] == ["A", "B", "C", "A"]
    assert float(opp.expected_edge_bps) >= 1.0


def test_triarb_detector_throttle(tmp_path):
    u = tmp_path / "token_universe.json"
    u.write_text(
        json.dumps(
            [{"family": "evm", "chain": "sepolia", "network": "testnet", "token_in": "A", "token_out": "B", "sizes": [100], "liquidity_usd": 10000}]
        ),
        encoding="utf-8",
    )
    d = TriArbDetector(_FakeRouter(), universe_path=str(u))
    first = d.on_event(_event())
    second = d.on_event(_event())
    assert isinstance(first, list)
    assert second == []


def test_triarb_detector_excludes_manual_deny(tmp_path, monkeypatch):
    u = tmp_path / "token_universe.json"
    u.write_text(
        json.dumps(
            [
                {
                    "family": "evm",
                    "chain": "sepolia",
                    "network": "testnet",
                    "token_in": "A",
                    "token_out": "B",
                    "sizes": [100],
                    "liquidity_usd": 10000,
                },
                {
                    "family": "evm",
                    "chain": "sepolia",
                    "network": "testnet",
                    "token_in": "B",
                    "token_out": "C",
                    "sizes": [100],
                    "liquidity_usd": 10000,
                },
                {
                    "family": "evm",
                    "chain": "sepolia",
                    "network": "testnet",
                    "token_in": "C",
                    "token_out": "A",
                    "sizes": [100],
                    "liquidity_usd": 10000,
                },
            ]
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "operator_state.json"
    st = default_state()
    st["risk_overrides"] = {**st["risk_overrides"], "deny_tokens": ["a"]}
    save_state(state_path, st, actor="test")
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))
    d = TriArbDetector(_FakeRouter(), universe_path=str(u))
    assert d.on_event(_event()) == []

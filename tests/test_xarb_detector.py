from __future__ import annotations

import json
import time

from ops.operator_state import default_state, save_state
from bot.core.opportunity_engine.types import MarketEvent
from bot.core.router import RouterQuoteResult
from bot.core.types_dex import Quote
from bot.detectors.xarb_detector import CrossDexArbScanDetector


def _q(dex: str, out: int, fee: float = 0.0) -> Quote:
    return Quote(
        dex=dex,
        expected_out=int(out),
        min_out=max(1, int(out) - 1),
        price_impact_bps=1.0,
        fee_estimate=float(fee),
        route_summary="x",
        quote_latency_ms=1.0,
    )


class _FakeRouter:
    def __init__(self):
        self.forward = {
            ("A", "B", 100): [
                RouterQuoteResult(dex="dex_a", quote=_q("dex_a", 120, fee=0.2), ok=True),
                RouterQuoteResult(dex="dex_b", quote=_q("dex_b", 118, fee=0.2), ok=True),
            ],
            ("A", "B", 200): [
                RouterQuoteResult(dex="dex_a", quote=_q("dex_a", 240, fee=0.4), ok=True),
                RouterQuoteResult(dex="dex_b", quote=_q("dex_b", 236, fee=0.4), ok=True),
            ],
        }

    def arb_scan(self, intent):
        return list(self.forward.get((intent.token_in, intent.token_out, int(intent.amount_in)), []))

    def route(self, intent):
        # reverse leg: B -> A, preferred sell dex decides output
        if intent.token_in == "B" and intent.token_out == "A":
            if str(intent.dex_preference) == "dex_b":
                out = int(intent.amount_in * 0.86) + 2
            elif str(intent.dex_preference) == "dex_a":
                out = int(intent.amount_in * 0.83)
            else:
                return None

            class _Sel:
                def __init__(self, dex, quote):
                    self.dex = dex
                    self.quote = quote
                    self.quote_table = []

                @property
                def candidates(self):
                    return self.quote_table

            return _Sel(str(intent.dex_preference), _q(str(intent.dex_preference), out, fee=0.2))
        return None


def _event() -> MarketEvent:
    return MarketEvent(
        id="evt1",
        ts=time.time(),
        family="evm",
        chain="sepolia",
        network="testnet",
        token_in="A",
        token_out="B",
        amount_hint=100,
        source="test",
        refs={},
    )


def test_xarb_detector_emits_opportunity_from_quote_table(tmp_path, monkeypatch):
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
                    "sizes": [100, 200],
                    "liquidity_usd": 10000,
                    "min_liquidity_usd": 1000,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XARB_MIN_EDGE_BPS", "2")
    monkeypatch.setenv("XARB_FEE_BPS", "0")
    d = CrossDexArbScanDetector(_FakeRouter(), universe_path=str(u))
    out = d.on_event(_event())
    assert len(out) == 1
    opp = out[0]
    assert opp.type == "xarb"
    assert opp.size_candidates == [100, 200]
    assert float(opp.expected_edge_bps) >= 2.0
    assert opp.constraints["best_dex"] == "dex_a"
    assert opp.constraints["sell_dex"] == "dex_b"


def test_xarb_detector_risk_gates_liquidity_and_edge(tmp_path, monkeypatch):
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
                    "liquidity_usd": 50,
                    "min_liquidity_usd": 1000,
                }
            ]
        ),
        encoding="utf-8",
    )
    d = CrossDexArbScanDetector(_FakeRouter(), universe_path=str(u))
    assert d.on_event(_event()) == []

    # edge threshold gate
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
                    "min_liquidity_usd": 1000,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XARB_MIN_EDGE_BPS", "10000")
    d2 = CrossDexArbScanDetector(_FakeRouter(), universe_path=str(u))
    assert d2.on_event(_event()) == []


def test_xarb_detector_excludes_manual_deny(tmp_path, monkeypatch):
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
                    "min_liquidity_usd": 1000,
                }
            ]
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "operator_state.json"
    st = default_state()
    st["risk_overrides"] = {**st["risk_overrides"], "deny_tokens": ["a"]}
    save_state(state_path, st, actor="test")
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(state_path))
    d = CrossDexArbScanDetector(_FakeRouter(), universe_path=str(u))
    assert d.on_event(_event()) == []

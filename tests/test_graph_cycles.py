from __future__ import annotations

from core.graph import build_token_graph, find_tri_cycles


def test_find_tri_cycles_detects_single_cycle():
    universe = [
        {"token_in": "A", "token_out": "B", "sizes": [100], "liquidity_usd": 1000},
        {"token_in": "B", "token_out": "C", "sizes": [100], "liquidity_usd": 1000},
        {"token_in": "C", "token_out": "A", "sizes": [100], "liquidity_usd": 1000},
    ]
    g = build_token_graph(universe, enabled_dexes=["univ2", "univ3"], bidirectional=False)
    cycles = find_tri_cycles(g, max_cycles=10, max_start_tokens=10)
    assert ("A", "B", "C") in cycles
    assert len(cycles) == 1


def test_find_tri_cycles_respects_limit():
    universe = [
        {"token_in": "A", "token_out": "B"},
        {"token_in": "B", "token_out": "C"},
        {"token_in": "C", "token_out": "A"},
        {"token_in": "A", "token_out": "D"},
        {"token_in": "D", "token_out": "E"},
        {"token_in": "E", "token_out": "A"},
    ]
    g = build_token_graph(universe, enabled_dexes=["univ2"], bidirectional=False)
    cycles = find_tri_cycles(g, max_cycles=1, max_start_tokens=10)
    assert len(cycles) == 1

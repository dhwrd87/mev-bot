from bot.candidate.schema import Candidate
from bot.sim.heuristic import HeuristicSimulator


def _candidate(*, tx_hash: str, edge_bps: float, gas: int) -> Candidate:
    return Candidate(
        chain="sepolia",
        tx_hash=tx_hash,
        seen_ts=1700000000000,
        to="0x0000000000000000000000000000000000000001",
        decoded_method=None,
        venue_tag="allowlist",
        estimated_gas=gas,
        estimated_edge_bps=edge_bps,
        sim_ok=False,
        pnl_est=0.0,
        decision="REJECT",
        reject_reason=None,
    )


def test_heuristic_simulator_is_deterministic_for_same_candidate():
    sim = HeuristicSimulator()
    c = _candidate(tx_hash="0x0000000000000000000000000000000000000000000000000000000000abc123", edge_bps=25.0, gas=21000)
    r1 = sim.simulate(c)
    r2 = sim.simulate(c)
    assert r1 == r2
    assert r1.sim_ok is True
    assert r1.error is None
    assert r1.pnl_est == 35.966249999999995


def test_heuristic_simulator_negative_case_is_stable():
    sim = HeuristicSimulator()
    c = _candidate(tx_hash="0x0", edge_bps=1.0, gas=21000)
    r = sim.simulate(c)
    assert r.sim_ok is False
    assert r.error is None
    assert r.pnl_est == -1.4025000000000003


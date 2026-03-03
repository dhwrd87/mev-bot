from __future__ import annotations

from bot.candidate.schema import Candidate
from bot.sim.base import SimResult


class HeuristicSimulator:
    """
    Fast deterministic paper-mode simulator.
    Uses only candidate fields so it is easy to replace with a fork simulator.
    """

    def __init__(self, *, eth_usd: float = 2500.0, default_gas_gwei: float = 25.0) -> None:
        self._eth_usd = float(eth_usd)
        self._default_gas_gwei = float(default_gas_gwei)

    def simulate(self, candidate: Candidate) -> SimResult:
        # Deterministic notional proxy from tx hash (stable per candidate)
        tx_tail = candidate.tx_hash[-6:] if candidate.tx_hash else "0"
        try:
            hash_mod = int(tx_tail, 16) % 10000
        except Exception:
            hash_mod = 0
        notional_usd = 100.0 + (float(hash_mod) * 2.5)  # 100..25097.5

        edge_gain_usd = notional_usd * (float(candidate.estimated_edge_bps) / 10000.0)
        gas_used = max(21_000, int(candidate.estimated_gas))
        gas_cost_usd = (self._default_gas_gwei * gas_used * 1e-9) * self._eth_usd

        # Another deterministic penalty term that can be replaced by real model later.
        price_impact_proxy = 1.0 + ((hash_mod % 500) / 10.0)  # 1.0..50.9
        pnl_est = edge_gain_usd - gas_cost_usd - (price_impact_proxy * 0.1)
        return SimResult(sim_ok=pnl_est > 0.0, pnl_est=float(pnl_est), error=None)


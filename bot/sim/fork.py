from __future__ import annotations

import os
from typing import Any

from bot.sim.base import SimResult


class ForkSimulator:
    """
    Skeleton for future fork-based simulation.
    """

    REQUIRED_ENV = (
        "FORK_RPC_URL",
        "FORK_BLOCK_TAG",
    )

    def __init__(self) -> None:
        self._missing = [k for k in self.REQUIRED_ENV if not os.getenv(k)]

    def simulate(self, candidate_or_plan: Any) -> SimResult:
        if self._missing:
            msg = "NotImplemented: fork simulator missing required env: " + ",".join(self._missing)
            return SimResult(sim_ok=False, pnl_est=0.0, error=msg)
        # Accept both candidate and plan objects for backward compatibility while
        # orchestration wiring standardizes on plan-level simulation.
        _ = candidate_or_plan
        return SimResult(
            sim_ok=False,
            pnl_est=0.0,
            error="NotImplemented: fork simulator wiring is not implemented yet",
        )

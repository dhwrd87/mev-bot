from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bot.candidate.schema import Candidate


@dataclass(frozen=True)
class SimResult:
    sim_ok: bool
    pnl_est: float
    error: str | None = None


class CandidateSimulator(Protocol):
    def simulate(self, candidate: Candidate) -> SimResult: ...


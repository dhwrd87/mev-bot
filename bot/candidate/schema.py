from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Candidate:
    chain: str
    tx_hash: str
    seen_ts: int
    to: Optional[str]
    decoded_method: Optional[str]
    venue_tag: str
    estimated_gas: int
    estimated_edge_bps: float
    sim_ok: bool
    pnl_est: float
    decision: str
    reject_reason: Optional[str]

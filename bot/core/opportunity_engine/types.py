from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MarketEvent:
    id: str
    ts: float
    family: str
    chain: str
    network: str
    token_in: str
    token_out: str
    amount_hint: int
    dex_hint: Optional[str] = None
    source: str = "unknown"
    refs: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Opportunity:
    id: str
    ts: float
    family: str
    chain: str
    network: str
    type: str
    size_candidates: List[int]
    expected_edge_bps: float
    confidence: float
    required_capabilities: List[str]
    constraints: Dict[str, Any]
    refs: Dict[str, str]


@dataclass(frozen=True)
class TradeLeg:
    dex: str
    token_in: str
    token_out: str
    amount_in: int
    expected_out: int
    min_out: int


@dataclass(frozen=True)
class TradePlan:
    id: str
    ts: float
    family: str
    chain: str
    network: str
    opportunity_id: str
    mode: str
    dex_pack: str
    ttl_s: int
    max_fee: float
    slippage_bps: int
    expected_profit_after_costs: float
    legs: List[TradeLeg]
    metadata: Dict[str, Any] = field(default_factory=dict)

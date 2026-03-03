from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


EventKind = Literal["block", "slot", "log", "pool_update", "quote_update"]


@dataclass(frozen=True)
class MarketEvent:
    id: str
    ts: float
    family: str
    chain: str
    network: str
    kind: EventKind
    block_number: Optional[int] = None
    slot: Optional[int] = None
    tx_hash: Optional[str] = None
    pool: Optional[str] = None
    dex: Optional[str] = None
    token_in: Optional[str] = None
    token_out: Optional[str] = None
    amount_in: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    refs: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Opportunity:
    id: str
    ts: float
    family: str
    chain: str
    network: str
    type: str
    signals: Dict[str, float]
    size_candidates: List[int]
    expected_edge_bps: float
    confidence: float
    required_capabilities: List[str]
    refs: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeIntent:
    family: str
    chain: str
    network: str
    token_in: str
    token_out: str
    amount_in: int
    slippage_bps: int
    ttl_s: int
    dex_preference: Optional[str]
    strategy: str


@dataclass(frozen=True)
class Quote:
    dex: str
    expected_out: int
    min_out: int
    price_impact_bps: float
    fee_estimate: float
    route_summary: str
    quote_latency_ms: float


@dataclass(frozen=True)
class TradeLeg:
    dex: str
    token_in: str
    token_out: str
    amount_in: int
    min_out: int
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TxPlan:
    family: str
    chain: str
    network: str
    dex: str
    mode: Literal["dryrun", "paper", "live"]
    legs: List[TradeLeg]
    raw_tx: Optional[str] = None
    instructions: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SimResult:
    ok: bool
    error_code: str = ""
    error_message: str = ""
    gas_estimate: Optional[int] = None
    compute_units: Optional[int] = None
    logs: Optional[List[str]] = None


@dataclass(frozen=True)
class TxReceiptOrSignature:
    tx_hash: Optional[str] = None
    signature: Optional[str] = None
    block_number: Optional[int] = None
    slot: Optional[int] = None
    status: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

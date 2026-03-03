from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


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
    strategy: str
    dex_preference: Optional[str] = None


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
class TxPlan:
    family: str
    chain: str
    dex: str
    value: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_tx: Optional[str] = None
    instruction_bundle: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SimResult:
    ok: bool
    error_code: str = ""
    error_message: str = ""
    gas_estimate: Optional[int] = None
    compute_units: Optional[int] = None
    logs: Optional[list[str]] = None

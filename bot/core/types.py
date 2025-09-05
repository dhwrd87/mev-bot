from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any, Optional, List

class ExecStatus(str, Enum):
    PENDING="pending"; SUCCESS="success"; FAILED="failed"; REVERTED="reverted"

@dataclass
class TransactionResult:
    status: ExecStatus
    tx_hash: Optional[str]
    mode: str                # 'stealth'|'hunter'|'hybrid'
    success: bool
    slippage: float
    sandwiched: bool
    gas_used: int
    notes: Dict[str, Any]

@dataclass
class Opportunity:
    chain: str
    detector: str            # 'sandwich'|'sniper'|...
    score: float             # 0..1
    liquidity_usd: float
    expected_profit_usd: float
    context: Dict[str, Any]

@dataclass
class Trade:
    chain: str
    token_in: str
    token_out: str
    amount_in: int
    expected_profit_usd: float
    estimated_gas_usd: float
    pool_liquidity_usd: float
    risk_score: float
    size_usd: float

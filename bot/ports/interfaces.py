from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol


class RpcClient(Protocol):
    async def get_tx(self, tx_hash: str) -> Any | None: ...
    async def gas_price(self) -> int: ...
    async def latest_block(self) -> Any: ...
    async def nonce(self, addr: str) -> int: ...


@dataclass
class SubmitResult:
    ok: bool
    tx_hash: Optional[str]
    relay: str
    error: Optional[str] = None


class PrivateOrderflowClient(Protocol):
    async def submit_tx(
        self,
        tx_hex: str,
        *,
        chain: str,
        traits: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SubmitResult: ...


class ReceiptProvider(Protocol):
    async def wait_for_receipt(self, tx_hash: str, *, timeout_s: int = 60) -> Optional[Dict[str, Any]]: ...


class OpportunityRepo(Protocol):
    async def insert_opportunity(self, row: Dict[str, Any]) -> int: ...


class TradeRepo(Protocol):
    async def insert_trade(self, row: Dict[str, Any]) -> int: ...
    async def update_trade_outcome(self, **kwargs: Any) -> None: ...


class RiskRepo(Protocol):
    async def record_state(self, row: Dict[str, Any]) -> None: ...


class AlertRepo(Protocol):
    async def send_alert(self, level: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None: ...

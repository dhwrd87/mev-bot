from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from bot.ports.interfaces import (
    RpcClient,
    PrivateOrderflowClient,
    ReceiptProvider,
    OpportunityRepo,
    TradeRepo,
    RiskRepo,
    AlertRepo,
    SubmitResult,
)


class FakeRpcClient(RpcClient):
    def __init__(self):
        self._txs: Dict[str, Any] = {}
        self._gas_price = 25_000_000_000
        self._latest_block = {"number": 1}
        self._nonce: Dict[str, int] = {}

    async def get_tx(self, tx_hash: str) -> Any | None:
        return self._txs.get(tx_hash)

    async def gas_price(self) -> int:
        return self._gas_price

    async def latest_block(self) -> Any:
        return self._latest_block

    async def nonce(self, addr: str) -> int:
        return self._nonce.get(addr.lower(), 0)


class FakePrivateOrderflowClient(PrivateOrderflowClient):
    def __init__(self, relay: str = "mev_blocker"):
        self.relay = relay
        self.submitted: List[str] = []

    async def submit_tx(
        self,
        tx_hex: str,
        *,
        chain: str,
        traits: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SubmitResult:
        self.submitted.append(tx_hex)
        return SubmitResult(ok=True, tx_hash="0xFAKE", relay=self.relay)


class FakeReceiptProvider(ReceiptProvider):
    def __init__(self, receipt: Optional[Dict[str, Any]] = None):
        self.receipt = receipt or {"status": 1, "blockNumber": 1}

    async def wait_for_receipt(self, tx_hash: str, *, timeout_s: int = 60) -> Optional[Dict[str, Any]]:
        return self.receipt


class FakeOpportunityRepo(OpportunityRepo):
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def insert_opportunity(self, row: Dict[str, Any]) -> int:
        row = dict(row)
        row["id"] = len(self.records) + 1
        self.records.append(row)
        return row["id"]


class FakeTradeRepo(TradeRepo):
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def insert_trade(self, row: Dict[str, Any]) -> int:
        row = dict(row)
        row["id"] = len(self.records) + 1
        self.records.append(row)
        return row["id"]

    async def update_trade_outcome(self, **kwargs: Any) -> None:
        trade_id = kwargs.get("id")
        if not trade_id:
            return
        for r in self.records:
            if r.get("id") == trade_id:
                for k, v in kwargs.items():
                    if v is not None:
                        r[k] = v
                return


class FakeRiskRepo(RiskRepo):
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def record_state(self, row: Dict[str, Any]) -> None:
        self.records.append(dict(row))


class FakeAlertRepo(AlertRepo):
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def send_alert(self, level: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.records.append({"level": level, "message": message, "payload": payload or {}})

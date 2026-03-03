from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class TransactionResult:
    success: bool
    tx_hash: str = ""
    mode: str = ""
    slippage: float = 0.0
    sandwiched: bool = False
    extra: Dict[str, Any] | None = None
    notes: Dict[str, Any] | None = None

    def __post_init__(self):
        if self.notes is None and self.extra is not None:
            self.notes = self.extra

class BaseStrategy(ABC):
    @abstractmethod
    async def evaluate(self, context: Dict[str, Any]) -> float:
        """Return a 0..1 profitability score."""
        ...

    @abstractmethod
    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        ...

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from bot.core.types_engine import (
    MarketEvent,
    Opportunity,
    SimResult,
    TradeIntent,
    TxPlan,
    TxReceiptOrSignature,
)


class Detector(ABC):
    @abstractmethod
    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        raise NotImplementedError


class Strategy(ABC):
    @abstractmethod
    def build_plan(self, opportunity: Opportunity) -> TxPlan:
        raise NotImplementedError


class Executor(ABC):
    @abstractmethod
    def simulate(self, plan: TxPlan) -> SimResult:
        raise NotImplementedError

    @abstractmethod
    def execute(self, plan: TxPlan) -> TxReceiptOrSignature:
        raise NotImplementedError


__all__ = [
    "Detector",
    "Strategy",
    "Executor",
    "MarketEvent",
    "Opportunity",
    "TradeIntent",
    "TxPlan",
    "SimResult",
    "TxReceiptOrSignature",
]

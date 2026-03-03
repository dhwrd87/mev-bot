from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from bot.core.opportunity_engine.types import MarketEvent, Opportunity


class BaseDetector(ABC):
    @abstractmethod
    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        raise NotImplementedError

    def name(self) -> str:
        return self.__class__.__name__

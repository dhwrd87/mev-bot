from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from bot.core.types_dex import TxPlan


class FlashloanProvider(ABC):
    @abstractmethod
    def supported_assets(self) -> Iterable[str]:
        raise NotImplementedError

    @abstractmethod
    def fee_bps(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def build_flashloan_wrapper(self, plan: TxPlan) -> TxPlan:
        raise NotImplementedError

    def name(self) -> str:
        return self.__class__.__name__.lower()

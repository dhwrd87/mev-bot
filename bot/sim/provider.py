from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, Optional, Dict, Any

from bot.exec.exact_output import ExactOutputParams


@dataclass
class SwapSimResult:
    ok: bool
    amount_in: Optional[int]
    reason: Optional[str] = None


@dataclass
class BundleSimResult:
    ok: bool
    endpoint: Optional[str]
    details: Dict[str, Any]


class SimProvider(Protocol):
    async def simulate_swap(self, params: ExactOutputParams, sender: str) -> SwapSimResult: ...
    async def simulate_bundle(
        self,
        signed_txs_hex: Sequence[str],
        target_block: Optional[int] = None,
        min_timestamp: Optional[int] = None,
        max_timestamp: Optional[int] = None,
        retries_per_endpoint: int = 1,
    ) -> BundleSimResult: ...

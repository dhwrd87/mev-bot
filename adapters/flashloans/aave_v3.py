from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from adapters.flashloans.base import FlashloanProvider
from bot.core.types_dex import TxPlan


DEFAULT_CONFIG_PATH = "config/flashloans/aave_v3.json"


def _norm_addr(v: str | None) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    return s.lower()


class AaveV3FlashloanProvider(FlashloanProvider):
    def __init__(
        self,
        *,
        chain: str,
        network: str = "mainnet",
        config_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.chain = str(chain or "").strip().lower()
        self.network = str(network or "").strip().lower()
        self._cfg = dict(config or self._load_config(config_path or os.getenv("AAVE_V3_CONFIG_PATH", DEFAULT_CONFIG_PATH)))
        per_chain = self._cfg.get("chains", {}).get(self.chain, {})
        if per_chain.get("network"):
            self.network = str(per_chain.get("network") or self.network).strip().lower()
        self.pool_address = _norm_addr(per_chain.get("pool"))
        if not self.pool_address:
            raise ValueError(f"missing_aave_v3_pool:{self.chain}")
        self._fee_bps = float(per_chain.get("fee_bps", self._cfg.get("default_fee_bps", 9.0)))
        self._assets = [_norm_addr(x) for x in (per_chain.get("assets") or []) if _norm_addr(x)]

        mode = str(per_chain.get("executor_mode") or "predeployed").strip().lower()
        if mode not in {"predeployed", "bytecode"}:
            raise ValueError(f"invalid_executor_mode:{mode}")
        self.executor_mode = mode
        self.executor_address = _norm_addr(per_chain.get("executor_address"))
        self.executor_bytecode = str(per_chain.get("executor_bytecode") or "").strip()

        if self.executor_mode == "predeployed" and not self.executor_address:
            raise ValueError(f"missing_executor_address:{self.chain}")
        if self.executor_mode == "bytecode" and not self.executor_bytecode:
            raise ValueError(f"missing_executor_bytecode:{self.chain}")

    @staticmethod
    def _load_config(path: str) -> Dict[str, Any]:
        p = Path(path)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"invalid_aave_v3_config:{e}") from e
        if not isinstance(raw, dict):
            raise ValueError("invalid_aave_v3_config_shape")
        return raw

    def name(self) -> str:
        return "aave_v3"

    def supported_assets(self) -> Iterable[str]:
        return list(self._assets)

    def fee_bps(self) -> float:
        return float(self._fee_bps)

    def estimate_fee_usd(self, *, amount_in_usd: float) -> float:
        return max(0.0, float(amount_in_usd)) * max(0.0, float(self._fee_bps)) / 10_000.0

    def build_flashloan_wrapper(self, plan: TxPlan) -> TxPlan:
        # Wrapper plan payload is metadata-driven so a sender/executor can choose:
        # - call predeployed executor, or
        # - deploy executor bytecode then call it.
        md = dict(plan.metadata or {})
        md["flashloan"] = {
            "provider": self.name(),
            "pool": self.pool_address,
            "fee_bps": float(self._fee_bps),
            "executor_mode": self.executor_mode,
            "executor_address": self.executor_address or None,
            "executor_bytecode": self.executor_bytecode or None,
        }
        ib = dict(plan.instruction_bundle or {})
        ib["flashloan_wrapper"] = {
            "provider": self.name(),
            "pool": self.pool_address,
            "executor_mode": self.executor_mode,
            "executor_address": self.executor_address or None,
        }
        return TxPlan(
            family=plan.family,
            chain=plan.chain,
            dex=plan.dex,
            value=plan.value,
            metadata=md,
            raw_tx=plan.raw_tx,
            instruction_bundle=ib,
        )

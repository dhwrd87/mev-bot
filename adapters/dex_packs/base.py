from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from bot.core.canonical import canonicalize_context
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan
from ops import metrics as ops_metrics


class DEXPack(ABC):
    def __init__(self, *, config: Optional[Dict[str, Any]] = None, instance_name: Optional[str] = None) -> None:
        self.config = config or {}
        self._instance_name = str(instance_name or "").strip().lower() or None

    @abstractmethod
    def quote(self, intent: TradeIntent) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        raise NotImplementedError

    @abstractmethod
    def simulate(self, plan: TxPlan) -> SimResult:
        raise NotImplementedError

    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def family_supported(self) -> str:
        raise NotImplementedError

    def chains_supported(self) -> Optional[list[str]]:
        return None

    def supports_context(self, *, family: str, chain: str) -> bool:
        ctx = canonicalize_context(family=family, chain=chain)
        fam, ch = ctx["family"], ctx["chain"]
        if fam != self.family_supported():
            return False
        chains = self.chains_supported()
        return True if not chains else ch in set(chains)


class _BaseStubPack(DEXPack):
    _name = "unknown"
    _family = "evm"
    _chains: Optional[list[str]] = None

    def name(self) -> str:
        return self._instance_name or self._name

    def family_supported(self) -> str:
        return self._family

    def chains_supported(self) -> Optional[list[str]]:
        return self._chains

    def quote(self, intent: TradeIntent) -> Quote:
        try:
            ops_metrics.record_dex_quote(family=intent.family, chain=intent.chain, dex=self.name())
            # Deterministic, paper-safe quote placeholder.
            expected_out = max(1, int(intent.amount_in * 0.99))
            min_out = max(1, int(expected_out * (1.0 - float(intent.slippage_bps) / 10_000.0)))
            quote = Quote(
                dex=self.name(),
                expected_out=expected_out,
                min_out=min_out,
                price_impact_bps=5.0,
                fee_estimate=float(intent.amount_in) * 0.003,
                route_summary=f"{intent.token_in}->{intent.token_out}",
                quote_latency_ms=1.0,
            )
            ops_metrics.record_dex_quote_latency(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                seconds=max(0.0, quote.quote_latency_ms / 1000.0),
            )
            route_hops = max(1, quote.route_summary.count("->"))
            ops_metrics.record_dex_route_hops(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                hops=route_hops,
            )
            return quote
        except Exception as e:
            ops_metrics.record_dex_quote_fail(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                reason=f"quote_error:{e}",
            )
            raise

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        try:
            md = {
                "strategy": intent.strategy,
                "slippage_bps": intent.slippage_bps,
                "route_summary": quote.route_summary,
                "chain": intent.chain,
                "network": intent.network,
                "config": self.config,
            }
            if self.family_supported() == "sol":
                return TxPlan(
                    family=intent.family,
                    chain=intent.chain,
                    dex=self.name(),
                    value=0,
                    metadata=md,
                    instruction_bundle={"instructions": ["swap"], "dex": self.name()},
                )
            return TxPlan(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                value=0,
                metadata=md,
                raw_tx="0x",
            )
        except Exception as e:
            ops_metrics.record_dex_build_fail(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                reason=f"build_error:{e}",
            )
            raise

    def simulate(self, plan: TxPlan) -> SimResult:
        try:
            if self.family_supported() == "sol":
                return SimResult(ok=True, compute_units=200_000, logs=["simulated"])
            return SimResult(ok=True, gas_estimate=180_000, logs=["simulated"])
        except Exception as e:
            ops_metrics.record_dex_sim_fail(
                family=plan.family,
                chain=plan.chain,
                dex=self.name(),
                reason=f"sim_error:{e}",
            )
            raise


class UniV2Pack(_BaseStubPack):
    _name = "univ2"
    _family = "evm"


class UniV3Pack(_BaseStubPack):
    _name = "univ3"
    _family = "evm"


class JupiterPack(_BaseStubPack):
    _name = "jupiter"
    _family = "sol"

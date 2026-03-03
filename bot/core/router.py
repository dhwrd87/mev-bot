from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

from adapters.dex_packs.registry import DEXPackRegistry
from bot.core.types_dex import Quote, TradeIntent
from ops import metrics as ops_metrics


@dataclass(frozen=True)
class RouterQuoteResult:
    dex: str
    quote: Optional[Quote]
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class RouterSelection:
    dex: str
    quote: Quote
    quote_table: List[RouterQuoteResult]

    @property
    def candidates(self) -> List[RouterQuoteResult]:
        # Backward-compatible alias for existing callers/tests.
        return self.quote_table


def _score_quote(quote: Quote) -> float:
    # Prefer higher output; include fee estimate if provided.
    return float(quote.expected_out) - float(quote.fee_estimate or 0.0)


class TradeRouter:
    def __init__(
        self,
        *,
        registry: DEXPackRegistry,
        quote_timeout_ms: Optional[int] = None,
        max_workers: Optional[int] = None,
    ) -> None:
        self.registry = registry
        self.quote_timeout_ms = int(
            quote_timeout_ms if quote_timeout_ms is not None else os.getenv("ROUTER_QUOTE_TIMEOUT_MS", "800")
        )
        self.max_workers = int(max_workers if max_workers is not None else os.getenv("ROUTER_MAX_WORKERS", "8"))

    def _eligible_pack_names(self, intent: TradeIntent) -> List[str]:
        names = sorted(self.registry.enabled_names())
        pref = (intent.dex_preference or "").strip().lower()
        if pref:
            return [pref] if pref in names else []
        return names

    def arb_scan(self, intent: TradeIntent) -> List[RouterQuoteResult]:
        names = self._eligible_pack_names(intent)
        fanout = len(names)
        ops_metrics.record_router_quote_fanout(family=intent.family, chain=intent.chain, fanout=fanout)
        if not names:
            return []

        timeout_s = max(0.05, float(self.quote_timeout_ms) / 1000.0)
        results: Dict[str, RouterQuoteResult] = {}

        def _run_quote(dex_name: str) -> RouterQuoteResult:
            pack = self.registry.get(dex_name)
            if pack is None:
                return RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason="pack_not_found")
            try:
                q = pack.quote(intent)
                return RouterQuoteResult(dex=dex_name, quote=q, ok=True, reason="")
            except Exception as e:
                return RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason=str(e))

        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(names)))) as ex:
            fut_to_name = {ex.submit(_run_quote, n): n for n in names}
            try:
                for fut in as_completed(fut_to_name, timeout=timeout_s):
                    dex_name = fut_to_name[fut]
                    try:
                        res = fut.result(timeout=0)
                    except TimeoutError:
                        res = RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason="quote_timeout")
                    except Exception as e:
                        res = RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason=str(e))
                    results[dex_name] = res
            except TimeoutError:
                pass

            # Mark not-yet-finished futures as timeout.
            for fut, dex_name in fut_to_name.items():
                if dex_name in results:
                    continue
                if fut.done():
                    try:
                        results[dex_name] = fut.result(timeout=0)
                    except Exception as e:
                        results[dex_name] = RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason=str(e))
                else:
                    results[dex_name] = RouterQuoteResult(dex=dex_name, quote=None, ok=False, reason="quote_timeout")
                    fut.cancel()

        out: List[RouterQuoteResult] = []
        for name in names:
            r = results.get(name, RouterQuoteResult(dex=name, quote=None, ok=False, reason="missing_result"))
            ops_metrics.record_router_quote(family=intent.family, chain=intent.chain, dex=name, ok=r.ok)
            out.append(r)

        return sorted(
            out,
            key=lambda r: (
                _score_quote(r.quote) if (r.ok and r.quote is not None) else float("-inf"),
                r.dex,
            ),
            reverse=True,
        )

    def route(self, intent: TradeIntent) -> Optional[RouterSelection]:
        quotes = self.arb_scan(intent)
        for item in quotes:
            if item.ok and item.quote is not None:
                ops_metrics.record_router_best_dex_selected(
                    family=intent.family,
                    chain=intent.chain,
                    dex=item.dex,
                )
                return RouterSelection(dex=item.dex, quote=item.quote, quote_table=quotes)
        return None

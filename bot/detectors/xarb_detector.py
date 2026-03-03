from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.core.opportunity_engine.types import MarketEvent, Opportunity
from bot.core.router import RouterQuoteResult, TradeRouter
from bot.core.types_dex import TradeIntent
from bot.detectors.base import BaseDetector
from ops import metrics as ops_metrics
from risk.risk_firewall import RiskFirewall


class CrossDexArbScanDetector(BaseDetector):
    def __init__(self, router: TradeRouter, *, universe_path: Optional[str] = None) -> None:
        self.router = router
        self.universe_path = (
            str(universe_path or os.getenv("XARB_TOKEN_UNIVERSE_PATH", "config/token_universe.json")).strip()
        )
        self.scan_interval_s = max(1.0, float(os.getenv("XARB_SCAN_INTERVAL_S", "5")))
        self.min_edge_bps = float(os.getenv("XARB_MIN_EDGE_BPS", "2.0"))
        self.min_liquidity_usd = float(os.getenv("XARB_MIN_LIQUIDITY_USD", "1000"))
        self.fee_bps = float(os.getenv("XARB_FEE_BPS", "0.0"))
        self.flashloan_fee_bps = float(os.getenv("XARB_FLASHLOAN_FEE_BPS", "0.0"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))
        self.operator_state_path = os.getenv("OPERATOR_STATE_PATH", os.getenv("OPERATOR_STATE_FILE", "ops/operator_state.json"))
        self._last_scan_ts = 0.0
        self._universe_cache: list[dict[str, Any]] = []
        self._universe_mtime: Optional[float] = None
        self._firewalls: Dict[str, RiskFirewall] = {}

    def _firewall(self, chain: str) -> RiskFirewall:
        c = str(chain or "unknown").strip().lower()
        fw = self._firewalls.get(c)
        if fw is None:
            fw = RiskFirewall(chain=c, operator_state_path=self.operator_state_path)
            self._firewalls[c] = fw
        return fw

    def _load_universe(self) -> list[dict[str, Any]]:
        p = Path(self.universe_path)
        try:
            st = p.stat()
        except Exception:
            self._universe_cache = []
            self._universe_mtime = None
            return []
        if self._universe_mtime == st.st_mtime and self._universe_cache:
            return self._universe_cache
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        rows: list[dict[str, Any]] = []
        for item in (raw if isinstance(raw, list) else []):
            if not isinstance(item, dict):
                continue
            token_in = str(item.get("token_in") or "").strip()
            token_out = str(item.get("token_out") or "").strip()
            if not token_in or not token_out:
                continue
            sizes_raw = item.get("sizes") or [100, 250, 500]
            sizes = sorted({int(x) for x in sizes_raw if int(x) > 0})
            if not sizes:
                continue
            rows.append(
                {
                    "family": str(item.get("family") or "evm").strip().lower(),
                    "chain": str(item.get("chain") or "sepolia").strip().lower(),
                    "network": str(item.get("network") or "testnet").strip().lower(),
                    "token_in": token_in,
                    "token_out": token_out,
                    "sizes": sizes,
                    "liquidity_usd": float(item.get("liquidity_usd") or 0.0),
                    "min_liquidity_usd": float(item.get("min_liquidity_usd") or self.min_liquidity_usd),
                }
            )
        self._universe_cache = rows
        self._universe_mtime = st.st_mtime
        return rows

    @staticmethod
    def _pair_key(a: str, b: str) -> str:
        return f"{a}->{b}"

    def _profit_edge_bps(self, *, buy_in: int, sell_out: int, buy_fee: float, sell_fee: float) -> float:
        base = float(max(1, int(buy_in)))
        gross = float(max(0, int(sell_out) - int(buy_in)))
        variable = base * max(0.0, self.fee_bps + self.flashloan_fee_bps) / 10_000.0
        net = gross - max(0.0, float(buy_fee)) - max(0.0, float(sell_fee)) - variable
        return (net / base) * 10_000.0

    def _scan_pair(self, *, family: str, chain: str, network: str, row: dict[str, Any]) -> list[Opportunity]:
        token_in = str(row["token_in"])
        token_out = str(row["token_out"])
        pair_id = f"{token_in}->{token_out}"
        liquidity = float(row.get("liquidity_usd") or 0.0)
        min_liq = float(row.get("min_liquidity_usd") or self.min_liquidity_usd)
        if liquidity < min_liq:
            ops_metrics.record_xarb_reject(family=family, chain=chain, dex_pair="unknown", reason="low_liquidity")
            return []

        tiny = int(min([int(x) for x in row.get("sizes", []) if int(x) > 0] or [100]))
        fw = self._firewall(chain)

        def _sim_buy() -> tuple[bool, str]:
            intent = TradeIntent(
                family=family,
                chain=chain,
                network=network,
                token_in=token_in,
                token_out=token_out,
                amount_in=max(1, tiny),
                slippage_bps=self.default_slippage_bps,
                ttl_s=self.default_ttl_s,
                strategy="risk_firewall",
            )
            table = self.router.arb_scan(intent)
            ok = any(x.ok and x.quote is not None for x in table)
            return ok, "ok" if ok else "no_buy_quote"

        def _sim_sell() -> tuple[bool, str]:
            intent = TradeIntent(
                family=family,
                chain=chain,
                network=network,
                token_in=token_out,
                token_out=token_in,
                amount_in=max(1, tiny),
                slippage_bps=self.default_slippage_bps,
                ttl_s=self.default_ttl_s,
                strategy="risk_firewall",
            )
            sel = self.router.route(intent)
            ok = bool(sel is not None and getattr(sel, "quote", None) is not None)
            return ok, "ok" if ok else "no_sell_quote"

        denied, decision = fw.should_exclude(
            token=token_in,
            pool=pair_id,
            metadata={
                "buy_tax_bps": row.get("buy_tax_bps"),
                "sell_tax_bps": row.get("sell_tax_bps"),
                "is_proxy": row.get("is_proxy"),
                "blacklist_enabled": row.get("blacklist_enabled"),
                "owner_can_block": row.get("owner_can_block"),
                "owner_renounced": row.get("owner_renounced", True),
            },
            simulate_buy=_sim_buy,
            simulate_sell=_sim_sell,
        )
        if denied:
            ops_metrics.record_xarb_reject(
                family=family,
                chain=chain,
                dex_pair=pair_id,
                reason="risk_firewall_deny",
            )
            return []

        candidate_sizes: list[int] = []
        best_edge = float("-inf")
        best_pair = ("", "")

        for size in [int(s) for s in row.get("sizes", []) if int(s) > 0]:
            buy_intent = TradeIntent(
                family=family,
                chain=chain,
                network=network,
                token_in=token_in,
                token_out=token_out,
                amount_in=int(size),
                slippage_bps=self.default_slippage_bps,
                ttl_s=self.default_ttl_s,
                strategy="opportunity_engine",
                dex_preference=None,
            )
            table = self.router.arb_scan(buy_intent)
            good: list[RouterQuoteResult] = [x for x in table if x.ok and x.quote is not None]
            if len(good) < 2:
                ops_metrics.record_xarb_reject(family=family, chain=chain, dex_pair="unknown", reason="insufficient_quotes")
                continue

            for buy in good:
                assert buy.quote is not None
                for sell in good:
                    if sell.dex == buy.dex:
                        continue
                    sell_intent = TradeIntent(
                        family=family,
                        chain=chain,
                        network=network,
                        token_in=token_out,
                        token_out=token_in,
                        amount_in=int(buy.quote.expected_out),
                        slippage_bps=self.default_slippage_bps,
                        ttl_s=self.default_ttl_s,
                        strategy="opportunity_engine",
                        dex_preference=sell.dex,
                    )
                    sel = self.router.route(sell_intent)
                    if sel is None or sel.quote is None:
                        ops_metrics.record_xarb_reject(
                            family=family,
                            chain=chain,
                            dex_pair=self._pair_key(buy.dex, sell.dex),
                            reason="no_sell_quote",
                        )
                        continue
                    pair = self._pair_key(buy.dex, sell.dex)
                    ops_metrics.record_xarb_scan(family=family, chain=chain, dex_pair=pair, ok=True)
                    edge_bps = self._profit_edge_bps(
                        buy_in=size,
                        sell_out=int(sel.quote.expected_out),
                        buy_fee=float(buy.quote.fee_estimate or 0.0),
                        sell_fee=float(sel.quote.fee_estimate or 0.0),
                    )
                    if edge_bps >= self.min_edge_bps:
                        candidate_sizes.append(int(size))
                        if edge_bps > best_edge:
                            best_edge = edge_bps
                            best_pair = (buy.dex, sell.dex)
                    else:
                        ops_metrics.record_xarb_reject(
                            family=family,
                            chain=chain,
                            dex_pair=pair,
                            reason="edge_below_threshold",
                        )

        if not candidate_sizes or best_edge == float("-inf"):
            return []
        sizes = sorted(set(candidate_sizes))
        buy_dex, sell_dex = best_pair
        pair_key = self._pair_key(buy_dex, sell_dex)
        ops_metrics.record_xarb_opportunity(family=family, chain=chain, dex_pair=pair_key)
        return [
            Opportunity(
                id=f"xarb:{chain}:{token_in}:{token_out}:{int(time.time() * 1000)}",
                ts=float(time.time()),
                family=family,
                chain=chain,
                network=network,
                type="xarb",
                size_candidates=sizes,
                expected_edge_bps=float(best_edge),
                confidence=0.70,
                required_capabilities=["quote", "build", "simulate"],
                constraints={
                    "token_in": token_in,
                    "token_out": token_out,
                    "best_dex": buy_dex,
                    "sell_dex": sell_dex,
                    "slippage_bps": self.default_slippage_bps,
                    "ttl_s": self.default_ttl_s,
                    "liquidity_usd": liquidity,
                    "min_liquidity_usd": min_liq,
                    "risk_classification": decision.classification,
                    "risk_reasons": list(decision.reasons),
                },
                refs={
                    "detector": self.name(),
                    "route_pair": pair_key,
                    "token_pair": f"{token_in}/{token_out}",
                    "risk_classification": decision.classification,
                    "risk_reasons": ",".join(decision.reasons),
                },
            )
        ]

    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        now = time.time()
        if now - self._last_scan_ts < self.scan_interval_s:
            return []
        self._last_scan_ts = now
        rows = self._load_universe()
        out: list[Opportunity] = []
        for row in rows:
            if row.get("family") != event.family:
                continue
            if row.get("chain") != event.chain:
                continue
            if row.get("network") != event.network:
                continue
            out.extend(self._scan_pair(family=event.family, chain=event.chain, network=event.network, row=row))
        return out

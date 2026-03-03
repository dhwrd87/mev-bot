from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.core.graph import build_token_graph, find_tri_cycles
from bot.core.opportunity_engine.types import MarketEvent, Opportunity
from bot.core.router import TradeRouter
from bot.core.types_dex import TradeIntent
from bot.detectors.base import BaseDetector
from ops import metrics as ops_metrics
from risk.risk_firewall import RiskFirewall


class TriArbDetector(BaseDetector):
    def __init__(self, router: TradeRouter, *, universe_path: Optional[str] = None) -> None:
        self.router = router
        self.universe_path = str(universe_path or os.getenv("TRIARB_UNIVERSE_PATH", "config/token_universe.json")).strip()
        self.scan_interval_s = max(1.0, float(os.getenv("TRIARB_SCAN_INTERVAL_S", "10")))
        self.max_cycles = max(1, int(os.getenv("TRIARB_MAX_CYCLES", "64")))
        self.max_start_tokens = max(1, int(os.getenv("TRIARB_MAX_START_TOKENS", "64")))
        self.min_edge_bps = float(os.getenv("TRIARB_MIN_EDGE_BPS", "2.0"))
        self.min_liquidity_usd = float(os.getenv("TRIARB_MIN_LIQUIDITY_USD", "1000"))
        self.fee_bps = float(os.getenv("TRIARB_FEE_BPS", "0.0"))
        self.flashloan_fee_bps = float(os.getenv("TRIARB_FLASHLOAN_FEE_BPS", "0.0"))
        self.default_slippage_bps = int(os.getenv("OPP_SLIPPAGE_BPS", "50"))
        self.default_ttl_s = int(os.getenv("OPP_TTL_S", "30"))
        self.operator_state_path = os.getenv("OPERATOR_STATE_PATH", os.getenv("OPERATOR_STATE_FILE", "ops/operator_state.json"))
        self._last_scan_ts = 0.0
        self._cache_rows: list[dict[str, Any]] = []
        self._cache_mtime: Optional[float] = None
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
            self._cache_rows, self._cache_mtime = [], None
            return []
        if self._cache_mtime == st.st_mtime and self._cache_rows:
            return self._cache_rows
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
            sizes = sorted({int(x) for x in item.get("sizes", []) if int(x) > 0})
            if not sizes:
                sizes = [100, 250, 500]
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
        self._cache_rows, self._cache_mtime = rows, st.st_mtime
        return rows

    def _size_candidates(self, base: int) -> list[int]:
        b = max(1, int(base))
        return sorted({max(1, int(round(b * 0.5))), b, max(1, int(round(b * 2.0)))})

    def _net_edge_bps(self, start_amount: int, end_amount: int, fee_sum: float) -> float:
        base = float(max(1, int(start_amount)))
        gross = float(max(0, int(end_amount) - int(start_amount)))
        variable = base * max(0.0, self.fee_bps + self.flashloan_fee_bps) / 10_000.0
        net = gross - variable - max(0.0, float(fee_sum))
        return (net / base) * 10_000.0

    def _pick_cycle_base(self, a: str, b: str, c: str, rows: list[dict[str, Any]]) -> int:
        sizes: list[int] = []
        for r in rows:
            tin = str(r.get("token_in") or "")
            tout = str(r.get("token_out") or "")
            if (tin, tout) in {(a, b), (b, c), (c, a), (b, a), (c, b), (a, c)}:
                sizes.extend([int(x) for x in r.get("sizes", []) if int(x) > 0])
        return min(sizes) if sizes else 100

    def _cycle_liquidity(self, a: str, b: str, c: str, rows: list[dict[str, Any]]) -> float:
        vals: list[float] = []
        for r in rows:
            tin = str(r.get("token_in") or "")
            tout = str(r.get("token_out") or "")
            if (tin, tout) in {(a, b), (b, c), (c, a), (b, a), (c, b), (a, c)}:
                vals.append(float(r.get("liquidity_usd") or 0.0))
        return min(vals) if vals else 0.0

    def _quote_leg(
        self,
        *,
        family: str,
        chain: str,
        network: str,
        token_in: str,
        token_out: str,
        amount_in: int,
    ) -> Optional[Any]:
        intent = TradeIntent(
            family=family,
            chain=chain,
            network=network,
            token_in=token_in,
            token_out=token_out,
            amount_in=int(amount_in),
            slippage_bps=self.default_slippage_bps,
            ttl_s=self.default_ttl_s,
            strategy="opportunity_engine",
        )
        return self.router.route(intent)

    def on_event(self, event: MarketEvent) -> List[Opportunity]:
        now = time.time()
        if now - self._last_scan_ts < self.scan_interval_s:
            return []
        self._last_scan_ts = now

        rows = [
            r
            for r in self._load_universe()
            if r.get("family") == event.family and r.get("chain") == event.chain and r.get("network") == event.network
        ]
        if not rows:
            return []
        enabled = []
        try:
            enabled = list(self.router.registry.enabled_names())  # type: ignore[attr-defined]
        except Exception:
            enabled = []
        graph = build_token_graph(rows, enabled_dexes=enabled or ["unknown"], bidirectional=True)
        cycles = find_tri_cycles(graph, max_cycles=self.max_cycles, max_start_tokens=self.max_start_tokens)

        t0 = time.perf_counter()
        out: list[Opportunity] = []
        for a, b, c in cycles:
            base_size = self._pick_cycle_base(a, b, c, rows)
            liq = self._cycle_liquidity(a, b, c, rows)
            if liq < self.min_liquidity_usd:
                ops_metrics.record_triarb_cycle_evaluated(family=event.family, chain=event.chain, dex_path="unknown", ok=False)
                continue

            fw = self._firewall(event.chain)
            pool_id = f"{a}>{b}>{c}>{a}"

            def _sim_buy() -> tuple[bool, str]:
                q = self._quote_leg(
                    family=event.family,
                    chain=event.chain,
                    network=event.network,
                    token_in=a,
                    token_out=b,
                    amount_in=max(1, int(base_size)),
                )
                ok = bool(q is not None and getattr(q, "quote", None) is not None)
                return ok, "ok" if ok else "buy_leg_failed"

            def _sim_sell() -> tuple[bool, str]:
                q = self._quote_leg(
                    family=event.family,
                    chain=event.chain,
                    network=event.network,
                    token_in=c,
                    token_out=a,
                    amount_in=max(1, int(base_size)),
                )
                ok = bool(q is not None and getattr(q, "quote", None) is not None)
                return ok, "ok" if ok else "sell_leg_failed"

            denied, decision = fw.should_exclude(
                token=a,
                pool=pool_id,
                metadata={"liquidity_usd": liq},
                simulate_buy=_sim_buy,
                simulate_sell=_sim_sell,
            )
            if denied:
                ops_metrics.record_triarb_cycle_evaluated(
                    family=event.family, chain=event.chain, dex_path="risk_firewall_deny", ok=False
                )
                continue

            q1 = self._quote_leg(
                family=event.family,
                chain=event.chain,
                network=event.network,
                token_in=a,
                token_out=b,
                amount_in=base_size,
            )
            if q1 is None or q1.quote is None:
                ops_metrics.record_triarb_cycle_evaluated(family=event.family, chain=event.chain, dex_path="unknown", ok=False)
                continue
            q2 = self._quote_leg(
                family=event.family,
                chain=event.chain,
                network=event.network,
                token_in=b,
                token_out=c,
                amount_in=int(q1.quote.expected_out),
            )
            if q2 is None or q2.quote is None:
                ops_metrics.record_triarb_cycle_evaluated(family=event.family, chain=event.chain, dex_path="unknown", ok=False)
                continue
            q3 = self._quote_leg(
                family=event.family,
                chain=event.chain,
                network=event.network,
                token_in=c,
                token_out=a,
                amount_in=int(q2.quote.expected_out),
            )
            if q3 is None or q3.quote is None:
                ops_metrics.record_triarb_cycle_evaluated(family=event.family, chain=event.chain, dex_path="unknown", ok=False)
                continue

            dex_path = f"{q1.dex}>{q2.dex}>{q3.dex}"
            fee_sum = float(q1.quote.fee_estimate or 0.0) + float(q2.quote.fee_estimate or 0.0) + float(
                q3.quote.fee_estimate or 0.0
            )
            edge_bps = self._net_edge_bps(base_size, int(q3.quote.expected_out), fee_sum=fee_sum)
            ops_metrics.record_triarb_cycle_evaluated(
                family=event.family, chain=event.chain, dex_path=dex_path, ok=edge_bps >= self.min_edge_bps
            )
            if edge_bps < self.min_edge_bps:
                continue

            sizes = self._size_candidates(base_size)
            out.append(
                Opportunity(
                    id=f"triarb:{event.chain}:{a}:{b}:{c}:{int(now * 1000)}",
                    ts=now,
                    family=event.family,
                    chain=event.chain,
                    network=event.network,
                    type="triarb",
                    size_candidates=sizes,
                    expected_edge_bps=float(edge_bps),
                    confidence=0.65,
                    required_capabilities=["quote", "build", "simulate"],
                    constraints={
                        "token_in": a,
                        "token_mid_1": b,
                        "token_mid_2": c,
                        "token_out": a,
                        "path_tokens": [a, b, c, a],
                        "path_dexes": [q1.dex, q2.dex, q3.dex],
                        "slippage_bps": self.default_slippage_bps,
                        "ttl_s": self.default_ttl_s,
                        "liquidity_usd": liq,
                        "risk_classification": decision.classification,
                        "risk_reasons": list(decision.reasons),
                    },
                    refs={
                        "detector": self.name(),
                        "route_tokens": f"{a}>{b}>{c}>{a}",
                        "route_dexes": dex_path,
                        "risk_classification": decision.classification,
                        "risk_reasons": ",".join(decision.reasons),
                    },
                )
            )
            ops_metrics.record_triarb_cycle_emitted(family=event.family, chain=event.chain, dex_path=dex_path)

        ops_metrics.record_triarb_compute_time(family=event.family, chain=event.chain, seconds=time.perf_counter() - t0)
        return out

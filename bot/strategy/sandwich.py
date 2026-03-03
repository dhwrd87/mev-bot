from __future__ import annotations

import inspect
import logging
import os
import time
from typing import Any, Dict, Optional, Sequence

from bot.candidate.schema import Candidate
from bot.core.types_engine import MarketEvent
from bot.core.telemetry import (
    sim_bundle_fail_total,
    sim_bundle_success_total,
    sim_bundle_total,
    trades_failed_total,
    trades_sent_total,
)
from bot.exec.bundle_builder import Bundle, BundleSubmitter, RawTx
from bot.exec.uniswap_v3 import build_exact_output_tx
from bot.sim.heuristic import HeuristicSimulator
from bot.strategy.base import BaseStrategy, TransactionResult

log = logging.getLogger("strategy.sandwich")


def _as_float(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _as_int(v: Any, d: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return d


def _now_s() -> int:
    return int(time.time())


def _pair_key(token_in: str, token_out: str) -> str:
    return f"{(token_in or '').lower()}:{(token_out or '').lower()}"


class SandwichStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        chain: str,
        signer: Any,
        rpc_client: Any,
        bundle_submitter: Optional[BundleSubmitter] = None,
        allowed_pairs: Optional[Sequence[str]] = None,
    ) -> None:
        self.chain = str(chain)
        self.signer = signer
        self.rpc_client = rpc_client
        self.bundle_submitter = bundle_submitter or BundleSubmitter(chain=self.chain)
        self.heuristic_sim = HeuristicSimulator()

        self.min_victim_usd = _as_float(os.getenv("SANDWICH_MIN_VICTIM_USD", "5000"), 5000.0)
        self.max_frontrun_pct = _as_float(os.getenv("SANDWICH_MAX_FRONTRUN_PCT", "0.03"), 0.03)
        self.priority_bump_gwei = _as_float(os.getenv("SANDWICH_PRIORITY_BUMP_GWEI", "2.0"), 2.0)
        self.min_profit_usd = _as_float(os.getenv("SANDWICH_MIN_PROFIT_USD", "10.0"), 10.0)
        self.victim_threshold = _as_float(os.getenv("SANDWICH_VICTIM_SCORE_THRESHOLD", "0.6"), 0.6)

        pairs = list(allowed_pairs or self._load_allowed_pairs_from_env())
        self.allowed_pairs = {p.strip().lower() for p in pairs if str(p).strip()}

    def _load_allowed_pairs_from_env(self) -> list[str]:
        raw = str(os.getenv("SANDWICH_ALLOWED_PAIRS", "*") or "*")
        return [x.strip() for x in raw.split(",") if x.strip()]

    async def evaluate(self, context: Dict[str, Any]) -> float:
        event = self._event_from_context(context)
        profit = max(0.0, self.estimate_profit(event))
        if self.min_profit_usd <= 0:
            return 1.0 if profit > 0 else 0.0
        return max(0.0, min(1.0, profit / (self.min_profit_usd * 5.0)))

    def _event_from_context(self, context: Dict[str, Any]) -> MarketEvent:
        if isinstance(context, MarketEvent):
            return context
        payload = dict(context.get("payload") or {})
        return MarketEvent(
            id=str(context.get("id", f"sandwich:{int(time.time() * 1000)}")),
            ts=float(context.get("ts", time.time())),
            family=str(context.get("family", "evm")),
            chain=str(context.get("chain", self.chain)),
            network=str(context.get("network", "testnet")),
            kind=str(context.get("kind", "quote_update")),  # type: ignore[arg-type]
            block_number=context.get("block_number"),
            tx_hash=context.get("tx_hash"),
            pool=context.get("pool"),
            dex=context.get("dex"),
            token_in=context.get("token_in") or payload.get("token_in"),
            token_out=context.get("token_out") or payload.get("token_out"),
            amount_in=_as_int(context.get("amount_in", payload.get("amount_in")), 0),
            payload=payload,
            refs=dict(context.get("refs") or {}),
        )

    def is_triggered_by(self, event: MarketEvent) -> bool:
        payload = event.payload or {}
        victim_score = _as_float(
            payload.get("sandwich_victim_score", payload.get("victim_score", payload.get("score", 0.0))),
            0.0,
        )
        if victim_score <= self.victim_threshold:
            return False

        token_in = (event.token_in or payload.get("token_in") or "").lower()
        token_out = (event.token_out or payload.get("token_out") or "").lower()
        pair = _pair_key(token_in, token_out)
        if "*" not in self.allowed_pairs and pair not in self.allowed_pairs:
            return False

        victim_usd = _as_float(payload.get("victim_amount_usd", payload.get("amount_in_usd", 0.0)), 0.0)
        if victim_usd < self.min_victim_usd:
            return False

        pool_liq_usd = _as_float(payload.get("pool_liquidity_usd", 0.0), 0.0)
        if pool_liq_usd <= 0:
            return False
        required_liq = victim_usd / max(self.max_frontrun_pct, 1e-9)
        return pool_liq_usd >= required_liq

    async def _resolve_chain_id(self, event: MarketEvent) -> int:
        payload = event.payload or {}
        cid = payload.get("chain_id")
        if cid is not None:
            return _as_int(cid, 0)
        w3 = self._resolve_w3()
        if w3 is not None:
            with_suppress = getattr(w3.eth, "chain_id", None)
            if with_suppress is not None:
                return _as_int(with_suppress, 0)
        return 11155111

    def _resolve_w3(self) -> Any:
        if hasattr(self.rpc_client, "w3s") and getattr(self.rpc_client, "w3s"):
            return self.rpc_client.w3s[0]
        if hasattr(self.rpc_client, "w3"):
            return getattr(self.rpc_client, "w3")
        return None

    async def _resolve_sender(self, event: MarketEvent) -> Optional[str]:
        payload = event.payload or {}
        sender = payload.get("sender") or payload.get("from")
        if sender:
            return str(sender)
        if hasattr(self.signer, "address"):
            return str(getattr(self.signer, "address"))
        return None

    async def _resolve_nonce(self, sender: str) -> int:
        if hasattr(self.rpc_client, "nonce"):
            n = self.rpc_client.nonce(sender)
            if inspect.isawaitable(n):
                n = await n
            return _as_int(n, 0)
        w3 = self._resolve_w3()
        if w3 is not None:
            return _as_int(w3.eth.get_transaction_count(sender), 0)
        return 0

    async def _sign_tx(self, tx: Dict[str, Any]) -> str:
        if hasattr(self.signer, "sign_transaction"):
            sig = self.signer.sign_transaction(tx, self.chain)
            if inspect.isawaitable(sig):
                sig = await sig
            return str(sig)
        if hasattr(self.signer, "sign_tx"):
            sig = self.signer.sign_tx(tx)
            if inspect.isawaitable(sig):
                sig = await sig
            return str(sig)
        raise RuntimeError("signer missing sign_transaction/sign_tx")

    def _gas_profile(self, event: MarketEvent) -> tuple[int, int]:
        payload = event.payload or {}
        gas_price_gwei = _as_float(
            payload.get("gas_price_gwei", payload.get("gas_price", payload.get("base_fee_gwei", 25.0))),
            25.0,
        )
        prio_gwei = _as_float(payload.get("priority_fee_gwei", 1.0), 1.0) + self.priority_bump_gwei
        max_fee = int(max(1.0, gas_price_gwei + self.priority_bump_gwei) * 1e9)
        max_prio = int(max(1.0, prio_gwei) * 1e9)
        return max_fee, max_prio

    def _frontrun_size(self, event: MarketEvent) -> int:
        payload = event.payload or {}
        pool_liq = _as_float(payload.get("pool_liquidity_usd", 0.0), 0.0)
        victim_usd = _as_float(payload.get("victim_amount_usd", payload.get("amount_in_usd", 0.0)), 0.0)
        our_usd = min(victim_usd, pool_liq * self.max_frontrun_pct)
        token_price = _as_float(payload.get("token_in_price_usd", 1.0), 1.0)
        return max(1, int(our_usd / max(token_price, 1e-9)))

    async def build_frontrun_tx(self, victim_event: MarketEvent, our_amount: int) -> RawTx:
        payload = victim_event.payload or {}
        token_in = str(victim_event.token_in or payload.get("token_in") or "")
        token_out = str(victim_event.token_out or payload.get("token_out") or "")
        sender = await self._resolve_sender(victim_event)
        if not sender:
            raise RuntimeError("missing sender for frontrun tx")

        chain_id = await self._resolve_chain_id(victim_event)
        nonce = await self._resolve_nonce(sender)
        fee = _as_int(payload.get("pool_fee", payload.get("pool_fee_bps", 3000)), 3000)
        deadline = _now_s() + _as_int(payload.get("ttl_s", 30), 30)
        max_fee, max_prio = self._gas_profile(victim_event)

        w3 = self._resolve_w3()
        if w3 is None:
            raise RuntimeError("rpc_client has no web3 provider")

        _, tx = build_exact_output_tx(
            w3,
            chain_id,
            token_in=token_in,
            token_out=token_out,
            fee=fee,
            recipient=sender,
            deadline=deadline,
            amount_out=max(1, int(our_amount)),
            amount_in_max=max(1, int(our_amount * 2)),
            sender=sender,
            nonce=nonce,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=max_prio,
            gas=_as_int(payload.get("frontrun_gas", 250000), 250000),
        )
        signed = await self._sign_tx(tx)
        return RawTx(hex=signed, from_addr=sender, nonce=nonce)

    async def build_backrun_tx(self, victim_event: MarketEvent, frontrun_amount_out: int) -> RawTx:
        payload = victim_event.payload or {}
        token_in = str(victim_event.token_out or payload.get("token_out") or "")
        token_out = str(victim_event.token_in or payload.get("token_in") or "")
        sender = await self._resolve_sender(victim_event)
        if not sender:
            raise RuntimeError("missing sender for backrun tx")

        chain_id = await self._resolve_chain_id(victim_event)
        nonce = await self._resolve_nonce(sender) + 1
        fee = _as_int(payload.get("pool_fee", payload.get("pool_fee_bps", 3000)), 3000)
        deadline = _now_s() + _as_int(payload.get("ttl_s", 30), 30)
        max_fee, max_prio = self._gas_profile(victim_event)

        w3 = self._resolve_w3()
        if w3 is None:
            raise RuntimeError("rpc_client has no web3 provider")

        _, tx = build_exact_output_tx(
            w3,
            chain_id,
            token_in=token_in,
            token_out=token_out,
            fee=fee,
            recipient=sender,
            deadline=deadline,
            amount_out=max(1, int(frontrun_amount_out)),
            amount_in_max=max(1, int(frontrun_amount_out * 2)),
            sender=sender,
            nonce=nonce,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=max_prio,
            gas=_as_int(payload.get("backrun_gas", 250000), 250000),
        )
        signed = await self._sign_tx(tx)
        return RawTx(hex=signed, from_addr=sender, nonce=nonce)

    async def build_bundle(self, victim_event: MarketEvent) -> Bundle:
        payload = victim_event.payload or {}
        victim_signed = str(
            payload.get("victim_signed_tx")
            or payload.get("raw_signed_tx_hex")
            or payload.get("target_signed_tx")
            or ""
        )
        if not victim_signed:
            raise RuntimeError("missing victim signed transaction")

        our_amount = self._frontrun_size(victim_event)
        fr = await self.build_frontrun_tx(victim_event, our_amount)
        br = await self.build_backrun_tx(victim_event, our_amount)

        current_block = victim_event.block_number
        if current_block is None and hasattr(self.rpc_client, "latest_block"):
            b = self.rpc_client.latest_block()
            if inspect.isawaitable(b):
                b = await b
            if isinstance(b, dict):
                current_block = _as_int(b.get("number"), 0)
            else:
                current_block = _as_int(getattr(b, "number", b), 0)
        current_block = _as_int(current_block, 0)
        return Bundle.new(
            txs=[fr, RawTx(victim_signed), br],
            current_block=current_block,
            skew=0,
        )

    def estimate_profit(self, victim_event: MarketEvent) -> float:
        payload = victim_event.payload or {}
        victim_usd = _as_float(payload.get("victim_amount_usd", payload.get("amount_in_usd", 0.0)), 0.0)
        pool_liq = _as_float(payload.get("pool_liquidity_usd", 0.0), 0.0)
        if victim_usd <= 0 or pool_liq <= 0:
            return -1.0

        frontrun_usd = min(victim_usd, pool_liq * self.max_frontrun_pct)
        # Approximate edge from size relative to pool depth.
        impact = max(0.0, min(0.25, victim_usd / max(pool_liq, 1e-9)))
        gross = frontrun_usd * impact * 0.7

        gas_price_gwei = _as_float(
            payload.get("gas_price_gwei", payload.get("gas_price", payload.get("base_fee_gwei", 25.0))),
            25.0,
        ) + self.priority_bump_gwei
        gas_units = _as_int(payload.get("bundle_gas_units", 750000), 750000)
        eth_usd = _as_float(payload.get("eth_usd", 2500.0), 2500.0)
        gas_cost = gas_units * gas_price_gwei * 1e-9 * eth_usd
        return float(gross - gas_cost)

    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        event = self._event_from_context(opportunity)
        if not self.is_triggered_by(event):
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="sandwich",
                sandwiched=False,
                notes={"reason": "not_triggered"},
            )

        profit_est = self.estimate_profit(event)
        if profit_est < self.min_profit_usd:
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="sandwich",
                sandwiched=False,
                notes={"reason": "profit_below_threshold", "profit_est_usd": profit_est},
            )

        try:
            bundle = await self.build_bundle(event)
        except Exception as e:
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="sandwich",
                sandwiched=False,
                notes={"reason": "bundle_build_failed", "error": str(e)},
            )

        sim_bundle_total.inc()
        # Heuristic pre-exec sim (fast guard)
        candidate = Candidate(
            chain=event.chain,
            tx_hash=event.tx_hash or event.id,
            seen_ts=max(0, int(event.ts * 1000)),
            to=event.pool,
            decoded_method=str(event.payload.get("selector") or ""),
            venue_tag=str(event.dex or "univ3"),
            estimated_gas=_as_int(event.payload.get("bundle_gas_units", 750000), 750000),
            estimated_edge_bps=max(0.0, (_as_float(profit_est, 0.0) / max(self.min_profit_usd, 1e-9)) * 10.0),
            sim_ok=True,
            pnl_est=profit_est,
            decision="ACCEPT",
            reject_reason=None,
        )
        sim_res = self.heuristic_sim.simulate(candidate)
        if not sim_res.sim_ok:
            sim_bundle_fail_total.labels(kind="heuristic_reject").inc()
            trades_failed_total.labels(chain_family=event.family, chain=event.chain, reason="heuristic_reject").inc()
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="sandwich",
                sandwiched=False,
                notes={"reason": "heuristic_reject", "sim_error": sim_res.error, "sim_pnl_est": sim_res.pnl_est},
            )
        sim_bundle_success_total.inc()

        tag = await self.bundle_submitter.submit(bundle)
        if not tag:
            trades_failed_total.labels(chain_family=event.family, chain=event.chain, reason="bundle_submit_failed").inc()
            return TransactionResult(
                success=False,
                tx_hash="",
                mode="sandwich",
                sandwiched=False,
                notes={"reason": "bundle_submit_failed", "profit_est_usd": profit_est},
            )

        trades_sent_total.labels(chain_family=event.family, chain=event.chain).inc()
        return TransactionResult(
            success=True,
            tx_hash=str(tag),
            mode="sandwich",
            sandwiched=True,
            notes={"bundle_tag": str(tag), "profit_est_usd": profit_est, "target_block": bundle.target_block},
        )


from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

import aiohttp
from redis.asyncio import Redis

from bot.candidate.schema import Candidate
from bot.core.chain_config import get_chain_config
from bot.net.instrumented_rpc import AsyncInstrumentedRpcClient
from bot.core.telemetry import (
    canonical_metric_labels,
    candidate_pipeline_seen_total,
    candidate_pipeline_detected_total,
    candidate_pipeline_decisions_total,
    pipeline_detector_hit_total,
    pipeline_detector_miss_total,
    pipeline_candidates_emitted_total,
)
from ops.metrics import (
    record_opportunity_attempted,
    record_opportunity_filled,
    record_opportunity_filtered,
    record_opportunity_seen,
)
from bot.sim.base import CandidateSimulator
from bot.sim.fork import ForkSimulator
from bot.sim.heuristic import HeuristicSimulator
from bot.storage.pg import (
    get_pool,
    ensure_candidates_table,
    ensure_mempool_pipeline_tables,
    insert_candidate_golden,
)

log = logging.getLogger("candidate-pipeline")

STREAM = os.getenv("REDIS_STREAM", "mempool:pending:txs")
GROUP = os.getenv("CANDIDATE_GROUP", "candidate-pipeline")
CONSUMER = os.getenv("CANDIDATE_CONSUMER") or os.getenv("HOSTNAME") or "candidate-worker-1"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ALLOWLIST_PATH = os.getenv("CANDIDATE_ALLOWLIST_PATH", "config/allowlist.json")
METHOD_SELECTORS = {s.strip().lower() for s in os.getenv("CANDIDATE_METHOD_SELECTORS", "0x").split(",") if s.strip()}

MIN_EDGE_BPS = float(os.getenv("MIN_EDGE_BPS", "5"))
MAX_GAS_GWEI = float(os.getenv("MAX_GAS_GWEI", "150"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0.10"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "0.05"))

PIPELINE_RPC_RPS = max(0.1, float(os.getenv("PIPELINE_RPC_RPS", "5")))
SIM_MODE = os.getenv("SIM_MODE", "heuristic").strip().lower()

_CHAIN_CFG = get_chain_config()
_CHAIN_LABELS = canonical_metric_labels(chain=_CHAIN_CFG.chain, chain_family=os.getenv("CHAIN_FAMILY", "evm"))
RPC_URLS = [_CHAIN_CFG.rpc_http] + _CHAIN_CFG.rpc_http_backups
_DAILY_LOSS_ACC = 0.0
OppSink = Optional[Callable[[dict], Awaitable[None]]]
_RPC_CLIENT = AsyncInstrumentedRpcClient(
    urls=RPC_URLS,
    family=os.getenv("CHAIN_FAMILY", "evm"),
    chain=_CHAIN_CFG.chain,
    rate_limit_rps=PIPELINE_RPC_RPS,
)


def _load_allowlist(path: str) -> Set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        payload = json.loads(p.read_text())
        contracts = payload.get("contracts", []) if isinstance(payload, dict) else []
        return {str(x).lower() for x in contracts if isinstance(x, str) and x.strip()}
    except Exception as e:
        log.warning("allowlist parse failed path=%s err=%s", path, e)
        return set()


def _to_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        if isinstance(v, str):
            return int(v, 0)
        return int(v)
    except Exception:
        return 0


def _field(fields: Dict[Any, Any], *names: str, default=None):
    for n in names:
        if n in fields:
            v = fields[n]
        elif n.encode() in fields:
            v = fields[n.encode()]
        else:
            continue
        if isinstance(v, (bytes, bytearray)):
            return v.decode(errors="ignore")
        return v
    return default


def _parse_entry(fields: Dict[Any, Any], entry_id: str) -> Tuple[str, int, Optional[str]]:
    tx_hash = str(_field(fields, "tx", "hash", default=""))
    ts_ms = _to_int(_field(fields, "ts_ms", default=0))
    if ts_ms <= 0:
        ts = _field(fields, "ts", default=None)
        if ts is not None:
            try:
                ts_ms = int(float(ts) * 1000.0)
            except Exception:
                ts_ms = 0
    if ts_ms <= 0:
        try:
            ts_ms = int(str(entry_id).split("-")[0])
        except Exception:
            ts_ms = int(time.time() * 1000)
    selector = _field(fields, "selector", default=None)
    return tx_hash, ts_ms, selector


async def _ensure_group(r: Redis) -> None:
    try:
        await r.xgroup_create(name=STREAM, groupname=GROUP, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            log.debug("xgroup_create notice: %s", e)


async def _fetch_tx(sess: aiohttp.ClientSession, tx_hash: str) -> Dict[str, Any] | None:
    if not tx_hash:
        return None
    res = await _RPC_CLIENT.call(
        sess,
        method="eth_getTransactionByHash",
        params=[tx_hash],
        timeout_s=5.0,
    )
    if not res.ok:
        return None
    return res.result if isinstance(res.result, dict) else None


def _detector_match(to_addr: str, value_wei: int, selector: str, allowlist: Set[str]) -> tuple[bool, str]:
    allowlist_hit = bool(to_addr and to_addr.lower() in allowlist and value_wei > 0)
    selector_match = False
    if selector:
        selector = selector.lower()
        selector_match = selector in METHOD_SELECTORS or "0x" in METHOD_SELECTORS or "*" in METHOD_SELECTORS
    if allowlist_hit:
        return True, "allowlist"
    if selector_match:
        return True, "selector"
    return False, "none"


def _build_simulator(mode: str) -> CandidateSimulator:
    if mode == "fork":
        return ForkSimulator()
    if mode != "heuristic":
        log.warning("unknown SIM_MODE=%s, falling back to heuristic", mode)
    return HeuristicSimulator()


def _decision(candidate: Candidate, gas_gwei: float, value_wei: int) -> tuple[str, Optional[str]]:
    global _DAILY_LOSS_ACC

    if candidate.estimated_edge_bps < MIN_EDGE_BPS:
        return "REJECT", "low_edge_bps"
    if gas_gwei > MAX_GAS_GWEI:
        return "REJECT", "high_gas_gwei"

    # Coarse risk proxy in paper mode using tx value as notional signal.
    value_eth = value_wei / 1e18
    if value_eth > (MAX_POSITION_SIZE * 100.0):
        return "REJECT", "max_position_size"

    if _DAILY_LOSS_ACC <= -(MAX_DAILY_LOSS * 10000.0):
        return "REJECT", "max_daily_loss"

    if not candidate.sim_ok:
        _DAILY_LOSS_ACC += candidate.pnl_est
        return "REJECT", "sim_negative"

    return "ACCEPT", None


def _to_market_event_dict(
    *,
    entry_id: str,
    seen_ts: int,
    tx_hash: str,
    venue_tag: str,
    to_addr: Optional[str],
    value_wei: int,
    selector: str,
    decision: str,
    reason: Optional[str],
    tx: Optional[Dict[str, Any]],
    edge_bps: float,
    gas_gwei: float,
    matched: bool,
) -> dict[str, Any]:
    return {
        "id": f"candidate:{entry_id}",
        "ts": float(seen_ts) / 1000.0,
        "family": _CHAIN_LABELS.get("family", "evm"),
        "chain": _CHAIN_LABELS.get("chain", _CHAIN_CFG.chain),
        "network": _CHAIN_LABELS.get("network", "testnet"),
        "kind": "quote_update",
        "tx_hash": tx_hash,
        "pool": to_addr,
        "dex": venue_tag or "unknown",
        "token_in": None,
        "token_out": None,
        "amount_in": value_wei,
        "payload": {
            "entry_id": entry_id,
            "decision": decision,
            "reject_reason": reason,
            "selector": selector or None,
            "detector_match": matched,
            "estimated_edge_bps": edge_bps,
            "gas_gwei": gas_gwei,
            "tx": tx,
        },
    }


async def _run_pipeline(opp_sink: OppSink = None) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    allowlist = _load_allowlist(ALLOWLIST_PATH)
    simulator = _build_simulator(SIM_MODE)

    r = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=False)
    await _ensure_group(r)

    pool = await get_pool()
    await ensure_mempool_pipeline_tables(pool)
    await ensure_candidates_table(pool)

    log.info(
        "candidate pipeline start stream=%s group=%s consumer=%s selectors=%s allowlist=%d sim_mode=%s",
        STREAM,
        GROUP,
        CONSUMER,
        sorted(METHOD_SELECTORS),
        len(allowlist),
        SIM_MODE,
    )

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
        while True:
            try:
                entries = await r.xreadgroup(
                    groupname=GROUP,
                    consumername=CONSUMER,
                    streams={STREAM: ">"},
                    count=50,
                    block=1000,
                )
            except Exception as e:
                log.warning("xreadgroup error: %s", e)
                await asyncio.sleep(1)
                continue

            if not entries:
                continue

            for stream_key, items in entries:
                for entry_id, fields in items:
                    tx_hash, seen_ts, selector = _parse_entry(fields, str(entry_id))
                    candidate_pipeline_seen_total.labels(**_CHAIN_LABELS).inc()
                    record_opportunity_seen(
                        family=os.getenv("CHAIN_FAMILY", "evm"),
                        chain=_CHAIN_CFG.chain,
                        dex="unknown",
                        strategy="candidate_pipeline",
                    )
                    tx = await _fetch_tx(sess, tx_hash)

                    to_addr = None
                    value_wei = 0
                    gas_wei = 0
                    if tx:
                        to_addr = tx.get("to")
                        value_wei = _to_int(tx.get("value"))
                        gas_wei = _to_int(tx.get("maxFeePerGas") or tx.get("gasPrice"))
                        inp = (tx.get("input") or tx.get("data") or "").lower()
                        if inp.startswith("0x") and len(inp) >= 10:
                            selector = inp[:10]
                    selector = selector or ""

                    matched, venue_tag = _detector_match(to_addr or "", value_wei, selector, allowlist)
                    if matched:
                        pipeline_detector_hit_total.labels(**_CHAIN_LABELS).inc()
                    else:
                        pipeline_detector_miss_total.labels(**_CHAIN_LABELS, reason="detector_miss").inc()

                    estimated_gas = _to_int(tx.get("gas")) if tx else 21000
                    edge_bps = (max(0.0, min(100.0, (gas_wei / 1e9) / 5.0 + (10.0 if matched else 0.0))))
                    gas_gwei = gas_wei / 1e9 if gas_wei > 0 else 0.0
                    candidate = Candidate(
                        chain=_CHAIN_CFG.chain,
                        tx_hash=tx_hash,
                        seen_ts=seen_ts,
                        to=to_addr,
                        decoded_method=selector if selector else None,
                        venue_tag=venue_tag,
                        estimated_gas=estimated_gas,
                        estimated_edge_bps=edge_bps,
                        sim_ok=False,
                        pnl_est=0.0,
                        decision="REJECT",
                        reject_reason=None,
                    )
                    sim_t0 = time.perf_counter()
                    sim_res = simulator.simulate(candidate)
                    sim_ms = (time.perf_counter() - sim_t0) * 1000.0
                    candidate.sim_ok = bool(sim_res.sim_ok)
                    candidate.pnl_est = float(sim_res.pnl_est)
                    log.info(
                        "sim_result mode=%s tx_hash=%s ms=%.2f sim_ok=%s pnl_est=%.6f error=%s",
                        SIM_MODE,
                        tx_hash,
                        sim_ms,
                        candidate.sim_ok,
                        candidate.pnl_est,
                        sim_res.error or "",
                    )

                    if not matched:
                        decision, reason = "REJECT", "detector_miss"
                        record_opportunity_filtered(
                            family=os.getenv("CHAIN_FAMILY", "evm"),
                            chain=_CHAIN_CFG.chain,
                            strategy="candidate_pipeline",
                            reason=reason,
                        )
                    else:
                        candidate_pipeline_detected_total.labels(**_CHAIN_LABELS, kind=venue_tag).inc()
                        record_opportunity_attempted(
                            family=os.getenv("CHAIN_FAMILY", "evm"),
                            chain=_CHAIN_CFG.chain,
                            dex=venue_tag or "unknown",
                            strategy="candidate_pipeline",
                        )
                        decision, reason = _decision(candidate, gas_gwei, value_wei)
                        if decision == "ACCEPT":
                            record_opportunity_filled(
                                family=os.getenv("CHAIN_FAMILY", "evm"),
                                chain=_CHAIN_CFG.chain,
                                dex=venue_tag or "unknown",
                                strategy="candidate_pipeline",
                            )
                        else:
                            record_opportunity_filtered(
                                family=os.getenv("CHAIN_FAMILY", "evm"),
                                chain=_CHAIN_CFG.chain,
                                strategy="candidate_pipeline",
                                reason=reason or "reject",
                            )

                    candidate.decision = decision
                    candidate.reject_reason = reason
                    if decision == "ACCEPT":
                        pipeline_candidates_emitted_total.labels(**_CHAIN_LABELS).inc()
                    candidate_pipeline_decisions_total.labels(
                        **_CHAIN_LABELS,
                        decision=decision,
                        reason=reason or "none",
                    ).inc()

                    await insert_candidate_golden(
                        pool,
                        {
                            "ts_ms": seen_ts,
                            "tx_hash": tx_hash,
                            "kind": "golden_path",
                            "score": edge_bps,
                            "notes": {
                                "entry_id": str(entry_id),
                                "detector_match": matched,
                                "stream": STREAM,
                            },
                            "chain": candidate.chain,
                            "seen_ts": candidate.seen_ts,
                            "to_addr": candidate.to,
                            "decoded_method": candidate.decoded_method,
                            "venue_tag": candidate.venue_tag,
                            "estimated_gas": candidate.estimated_gas,
                            "estimated_edge_bps": candidate.estimated_edge_bps,
                            "sim_ok": candidate.sim_ok,
                            "pnl_est": candidate.pnl_est,
                            "decision": candidate.decision,
                            "reject_reason": candidate.reject_reason,
                        },
                    )

                    if decision == "ACCEPT" and opp_sink is not None:
                        candidate_dict = _to_market_event_dict(
                            entry_id=str(entry_id),
                            seen_ts=seen_ts,
                            tx_hash=tx_hash,
                            venue_tag=venue_tag,
                            to_addr=to_addr,
                            value_wei=value_wei,
                            selector=selector,
                            decision=decision,
                            reason=reason,
                            tx=tx,
                            edge_bps=edge_bps,
                            gas_gwei=gas_gwei,
                            matched=matched,
                        )
                        try:
                            await opp_sink(candidate_dict)
                        except Exception as e:
                            log.warning("opp_sink dispatch failed tx_hash=%s err=%s", tx_hash, e)

                    with contextlib.suppress(Exception):
                        await r.xack(stream_key, GROUP, entry_id)


def build_candidate_pipeline(opp_sink: OppSink = None) -> Callable[[], Awaitable[None]]:
    async def _run() -> None:
        await _run_pipeline(opp_sink=opp_sink)

    return _run


async def run() -> None:
    await _run_pipeline(opp_sink=None)


if __name__ == "__main__":
    asyncio.run(run())

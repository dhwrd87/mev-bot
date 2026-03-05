"""Redis mempool stream worker that detects and processes trading opportunities.

This worker bridges the mempool stream and orchestrator execution pipeline:
it consumes pending mempool transactions, runs configured detectors, emits
Prometheus metrics, forwards opportunities to the trading orchestrator, and
handles graceful shutdown for long-running operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import signal
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest
from redis.asyncio import Redis

from bot.core.opportunity_engine.types import MarketEvent, Opportunity as EngineOpportunity
from bot.detectors.triarb_detector import TriArbDetector
from bot.detectors.xarb_detector import CrossDexArbScanDetector

LOG = logging.getLogger("opportunity-processor")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
REDIS_STREAM = os.getenv("REDIS_STREAM", "mempool:pending:txs")
REDIS_GROUP = os.getenv("REDIS_GROUP", "opportunity_processor")

READ_BATCH_SIZE = 10
READ_BLOCK_MS = 1000

def _counter(name: str, documentation: str, labels: list[str]) -> Counter:
    try:
        return Counter(name, documentation, labels)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing  # type: ignore[return-value]


def _gauge(name: str, documentation: str, labels: list[str]) -> Gauge:
    try:
        return Gauge(name, documentation, labels)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing  # type: ignore[return-value]


def _histogram(name: str, documentation: str, labels: list[str], buckets: list[float]) -> Histogram:
    try:
        return Histogram(name, documentation, labels, buckets=buckets)
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if existing is None:
            raise
        return existing  # type: ignore[return-value]


opportunities_detected_total = _counter(
    "opportunities_detected_total",
    "Total opportunities detected from mempool stream messages",
    ["family", "chain", "type", "detector"],
)
opportunities_processed_total = _counter(
    "opportunities_processed_total",
    "Total opportunities processed by orchestrator outcome",
    ["family", "chain", "type", "outcome"],
)
opportunity_latency_ms = _histogram(
    "opportunity_latency_ms",
    "Latency from detection to processing decision in milliseconds",
    ["family", "chain", "type"],
    buckets=[1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)
opportunity_queue_depth = _gauge(
    "opportunity_queue_depth",
    "Approximate Redis mempool stream queue depth",
    ["family", "chain"],
)


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _infer_network(chain: str) -> str:
    """Infer network label from chain string for MarketEvent construction."""
    c = str(chain or "").strip().lower()
    if c in {"ethereum-mainnet", "ethereum", "mainnet", "arbitrum-mainnet", "base-mainnet", "polygon-mainnet"}:
        return "mainnet"
    return "testnet"


def _as_str(value: Any, default: str = "") -> str:
    """Normalize values from Redis payloads into strings."""
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        return value.decode(errors="ignore")
    return str(value)


def _normalize_stream_id(value: Any) -> str:
    """Normalize Redis stream ids, including stringified byte repr forms."""
    raw = _as_str(value)
    if raw.startswith("b'") and raw.endswith("'"):
        raw = raw[2:-1]
    if raw.startswith('b"') and raw.endswith('"'):
        raw = raw[2:-1]
    return raw


def _normalize_message_fields(fields: Mapping[Any, Any]) -> dict[str, Any]:
    """Decode Redis stream fields into a normalized transaction payload."""
    normalized: dict[str, Any] = {}
    for k, v in fields.items():
        key = _as_str(k).strip()
        value: Any = _as_str(v)
        if key in {"value", "gas_price"}:
            with contextlib.suppress(Exception):
                value = int(str(value), 0)
        normalized[key] = value

    tx_hash = normalized.get("hash") or normalized.get("tx") or ""
    return {
        "hash": _as_str(tx_hash),
        "chain": _as_str(normalized.get("chain"), "unknown"),
        "family": _as_str(normalized.get("family"), "evm"),
        "from": _as_str(normalized.get("from")),
        "to": _as_str(normalized.get("to")),
        "value": int(normalized.get("value", 0) or 0),
        "data": _as_str(normalized.get("data")),
        "gas_price": int(normalized.get("gas_price", 0) or 0),
        "token_in": _as_str(normalized.get("token_in")),
        "token_out": _as_str(normalized.get("token_out")),
        "dex": _as_str(normalized.get("dex")),
    }


class SandwichDetector:
    """Fallback sandwich detector.

    This no-op detector keeps the detector pipeline shape stable when a concrete
    sandwich detector implementation is not available in ``bot.detectors``.
    """

    def detect(self, _tx: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def name() -> str:
        return "SandwichDetector"


class TradingOrchestratorAdapter:
    """Compatibility wrapper exposing ``handle_opportunity`` across orchestrator APIs."""

    def __init__(self, orchestrator: Any) -> None:
        self._orchestrator = orchestrator

    async def handle_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        """Process one opportunity and normalize the result payload."""
        if hasattr(self._orchestrator, "handle_opportunity"):
            result = self._orchestrator.handle_opportunity(opportunity)
            result = await result if inspect.isawaitable(result) else result
            return self._normalize_result(result)

        if hasattr(self._orchestrator, "handle"):
            result = self._orchestrator.handle(opportunity)
            result = await result if inspect.isawaitable(result) else result
            return self._normalize_result(result)

        if hasattr(self._orchestrator, "process_opportunity"):
            engine_opp = self._to_engine_opportunity(opportunity)
            decision = self._orchestrator.process_opportunity(engine_opp)
            status = _as_str(getattr(decision, "status", "unknown"), "unknown")
            reason = _as_str(getattr(decision, "reason", "unknown"), "unknown")
            return {"outcome": status, "reason": reason}

        return {"outcome": "error", "reason": "orchestrator_missing_handler"}

    @staticmethod
    def _normalize_result(result: Any) -> dict[str, Any]:
        if isinstance(result, Mapping):
            if "outcome" in result:
                return dict(result)
            if "status" in result:
                out = dict(result)
                out["outcome"] = _as_str(result.get("status"), "unknown")
                return out
            if "ok" in result:
                out = dict(result)
                out["outcome"] = "accepted" if bool(result.get("ok")) else "rejected"
                return out
            return {"outcome": "unknown", "result": dict(result)}
        if result is None:
            return {"outcome": "unknown"}
        return {"outcome": _as_str(result, "unknown")}

    @staticmethod
    def _to_engine_opportunity(opp: Mapping[str, Any]) -> EngineOpportunity:
        raw = opp.get("_engine_opportunity")
        if isinstance(raw, EngineOpportunity):
            return raw

        size_usd = max(1, int(float(opp.get("size_usd", 1.0) or 1.0)))
        expected_profit = float(opp.get("expected_profit_usd", 0.0) or 0.0)
        edge_bps = float(opp.get("expected_edge_bps", (expected_profit / size_usd) * 10_000.0))
        chain = _as_str(opp.get("chain"), "unknown")
        family = _as_str(opp.get("family"), "evm")

        return EngineOpportunity(
            id=_as_str(opp.get("id"), f"opp:{int(time.time() * 1000)}"),
            ts=float(opp.get("ts", time.time()) or time.time()),
            family=family,
            chain=chain,
            network=_as_str(opp.get("network"), _infer_network(chain)),
            type=_as_str(opp.get("type"), "unknown"),
            size_candidates=[size_usd],
            expected_edge_bps=edge_bps,
            confidence=float(opp.get("confidence", 0.5) or 0.5),
            required_capabilities=["quote", "build", "simulate"],
            constraints={
                "token_in": _as_str(opp.get("token_in")),
                "token_out": _as_str(opp.get("token_out")),
                "best_dex": _as_str(opp.get("dex"), "unknown"),
            },
            refs={"source": "opportunity_processor"},
        )


class _NoopTradingOrchestrator:
    """Fallback orchestrator used when full orchestrator construction fails."""

    async def handle_opportunity(self, _opportunity: Mapping[str, Any]) -> dict[str, Any]:
        return {"outcome": "noop", "reason": "orchestrator_unavailable"}


async def create_orchestrator() -> Any:
    """Create the trading orchestrator instance.

    Order of resolution:
    1. ``bot.orchestration.trading_orchestrator.create_orchestrator`` factory.
    2. ``bot.orchestration.trading_orchestrator.TradingOrchestrator`` constructor.
    3. Unified opportunity orchestrator fallback built from current router/registry.
    4. No-op orchestrator fallback to keep worker alive.
    """
    try:
        from bot.orchestration import trading_orchestrator as trading_mod  # type: ignore

        if hasattr(trading_mod, "create_orchestrator"):
            created = getattr(trading_mod, "create_orchestrator")()
            return await created if inspect.isawaitable(created) else created
        if hasattr(trading_mod, "TradingOrchestrator"):
            return getattr(trading_mod, "TradingOrchestrator")()
    except Exception as exc:
        LOG.info("orchestrator_import_fallback reason=%s", exc)

    try:
        from adapters.dex_packs.registry import DEXPackRegistry
        from bot.core.chain_config import get_chain_config
        from bot.core.router import TradeRouter
        from bot.orchestrator.opportunity_orchestrator import OpportunityOrchestrator

        cfg = get_chain_config()
        family = "sol" if cfg.chain == "solana" else "evm"
        network = _infer_network(cfg.chain)
        registry = DEXPackRegistry()
        registry.reload(family=family, chain=cfg.chain, network=network)
        router = TradeRouter(registry=registry)
        return OpportunityOrchestrator(router=router, registry=registry, detectors=[])
    except Exception as exc:
        LOG.warning("orchestrator_build_failed reason=%s", exc, exc_info=True)
        return _NoopTradingOrchestrator()


def _extract_router(orchestrator: Any) -> Any:
    """Best-effort extraction of TradeRouter from orchestrator object."""
    if hasattr(orchestrator, "router"):
        return getattr(orchestrator, "router")
    inner = getattr(orchestrator, "_orchestrator", None)
    if inner is not None and hasattr(inner, "router"):
        return getattr(inner, "router")
    return None


def _build_detectors(router: Any) -> list[Any]:
    """Initialize detector list with available detector implementations."""
    detectors: list[Any] = []

    if router is not None:
        with contextlib.suppress(Exception):
            detectors.append(TriArbDetector(router))
        with contextlib.suppress(Exception):
            detectors.append(CrossDexArbScanDetector(router))
    else:
        LOG.warning("detector_router_unavailable; skipping TriArbDetector/CrossDexArbScanDetector")

    try:
        from bot.detectors.sandwich_detector import SandwichDetector as ConcreteSandwichDetector  # type: ignore

        detectors.append(ConcreteSandwichDetector())
    except Exception:
        detectors.append(SandwichDetector())

    return detectors


def _metric_labels_from_tx(tx: Mapping[str, Any]) -> tuple[str, str]:
    return _as_str(tx.get("family"), "unknown"), _as_str(tx.get("chain"), "unknown")


def _to_market_event(entry_id: str, tx: Mapping[str, Any]) -> MarketEvent:
    """Construct a MarketEvent for detector APIs that expect event-driven input."""
    chain = _as_str(tx.get("chain"), "unknown")
    family = _as_str(tx.get("family"), "evm")
    return MarketEvent(
        id=f"mempool:{entry_id}",
        ts=time.time(),
        family=family,
        chain=chain,
        network=_infer_network(chain),
        token_in=_as_str(tx.get("token_in") or tx.get("from")),
        token_out=_as_str(tx.get("token_out") or tx.get("to")),
        amount_hint=int(tx.get("value", 0) or 0),
        dex_hint=_as_str(tx.get("dex")) or None,
        source="mempool_stream",
        refs={"tx_hash": _as_str(tx.get("hash"))},
    )


def _detector_name(detector: Any) -> str:
    if hasattr(detector, "name"):
        name = detector.name()
        if isinstance(name, str) and name.strip():
            return name.strip()
    return detector.__class__.__name__


def _normalize_detected_opportunities(
    detected: Any,
    *,
    detector_name: str,
    tx: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize detector outputs into orchestrator-ready dictionaries."""
    if detected is None:
        return []
    if not isinstance(detected, list):
        detected = [detected]

    out: list[dict[str, Any]] = []
    for item in detected:
        if item is None:
            continue

        if isinstance(item, EngineOpportunity):
            size_usd = float(min(item.size_candidates) if item.size_candidates else 0.0)
            expected_profit_usd = max(0.0, size_usd * float(item.expected_edge_bps) / 10_000.0)
            out.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "expected_profit_usd": expected_profit_usd,
                    "size_usd": size_usd,
                    "token_in": _as_str(item.constraints.get("token_in")),
                    "token_out": _as_str(item.constraints.get("token_out")),
                    "dex": _as_str(item.constraints.get("best_dex"), "unknown"),
                    "detector": detector_name,
                    "family": item.family or _as_str(tx.get("family"), "unknown"),
                    "chain": item.chain or _as_str(tx.get("chain"), "unknown"),
                    "_engine_opportunity": item,
                }
            )
            continue

        if is_dataclass(item):
            item = asdict(item)

        if not isinstance(item, Mapping):
            continue

        type_name = _as_str(item.get("type"), "unknown")
        size_usd = float(item.get("size_usd", item.get("size", 0.0)) or 0.0)
        expected_profit_usd = float(item.get("expected_profit_usd", 0.0) or 0.0)
        out.append(
            {
                "id": _as_str(item.get("id") or item.get("opportunity_id")),
                "type": type_name,
                "expected_profit_usd": expected_profit_usd,
                "size_usd": size_usd,
                "token_in": _as_str(item.get("token_in")),
                "token_out": _as_str(item.get("token_out")),
                "dex": _as_str(item.get("dex"), "unknown"),
                "detector": _as_str(item.get("detector"), detector_name),
                "family": _as_str(item.get("family"), _as_str(tx.get("family"), "unknown")),
                "chain": _as_str(item.get("chain"), _as_str(tx.get("chain"), "unknown")),
            }
        )

    return out


def _extract_outcome(result: Mapping[str, Any]) -> str:
    """Extract a normalized outcome label from orchestrator response."""
    if "outcome" in result:
        return _as_str(result.get("outcome"), "unknown")
    if "status" in result:
        return _as_str(result.get("status"), "unknown")
    if "ok" in result:
        return "accepted" if bool(result.get("ok")) else "rejected"
    return "unknown"


class OpportunityProcessor:
    """Consumes mempool stream messages and routes detected opportunities to orchestrator."""

    def __init__(
        self,
        *,
        redis_url: str,
        stream: str,
        group: str,
        orchestrator: Any,
        detectors: Iterable[Any],
    ) -> None:
        self.redis_url = redis_url
        self.stream = stream
        self.group = group
        self.consumer = f"processor_{os.getpid()}"
        self.redis = Redis.from_url(redis_url, encoding="utf-8", decode_responses=False)
        self.orchestrator = TradingOrchestratorAdapter(orchestrator)
        self.detectors = list(detectors)
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        """Run the read-process-ack loop until shutdown."""
        await self._ensure_group()
        self._install_signal_handlers()
        LOG.info(
            "processor_start stream=%s group=%s consumer=%s detectors=%s",
            self.stream,
            self.group,
            self.consumer,
            ",".join(_detector_name(d) for d in self.detectors) or "none",
        )

        try:
            while not self.stop_event.is_set():
                try:
                    response = await self.redis.xreadgroup(
                        groupname=self.group,
                        consumername=self.consumer,
                        streams={self.stream: ">"},
                        count=READ_BATCH_SIZE,
                        block=READ_BLOCK_MS,
                    )
                except Exception:
                    LOG.exception("stream_read_error stream=%s group=%s", self.stream, self.group)
                    await asyncio.sleep(0.5)
                    continue

                if not response:
                    continue

                label_pairs: set[tuple[str, str]] = set()
                for _stream_name, entries in response:
                    for entry_id_raw, fields in entries:
                        entry_id = _as_str(entry_id_raw)
                        await self._process_entry(entry_id=entry_id, fields=fields)
                        tx = _normalize_message_fields(fields)
                        label_pairs.add(_metric_labels_from_tx(tx))
                await self._update_queue_depth(label_pairs)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop processing, close Redis, and flush metrics."""
        self.stop_event.set()
        with contextlib.suppress(Exception):
            await self.redis.close()
        self._flush_metrics()
        LOG.info("processor_shutdown_complete consumer=%s", self.consumer)

    async def _ensure_group(self) -> None:
        """Ensure the Redis stream consumer group exists."""
        try:
            await self.redis.xgroup_create(name=self.stream, groupname=self.group, id="$", mkstream=True)
            LOG.info("stream_group_created stream=%s group=%s", self.stream, self.group)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                LOG.warning("stream_group_create_notice stream=%s group=%s err=%s", self.stream, self.group, exc)

    async def _process_entry(self, *, entry_id: str, fields: Mapping[Any, Any]) -> None:
        """Process one Redis stream entry and acknowledge it afterward."""
        tx = _normalize_message_fields(fields)
        tx_hash = _as_str(tx.get("hash"))
        family, chain = _metric_labels_from_tx(tx)

        try:
            event = _to_market_event(entry_id, tx)
            all_opps: list[dict[str, Any]] = []

            for detector in self.detectors:
                det_name = _detector_name(detector)
                try:
                    detected = await self._invoke_detector(detector, tx=tx, event=event)
                except Exception:
                    LOG.exception(
                        "detector_error detector=%s tx_hash=%s family=%s chain=%s",
                        det_name,
                        tx_hash,
                        family,
                        chain,
                    )
                    continue

                normalized = _normalize_detected_opportunities(detected, detector_name=det_name, tx=tx)
                all_opps.extend(normalized)
                for opp in normalized:
                    opportunities_detected_total.labels(
                        family=_as_str(opp.get("family"), family),
                        chain=_as_str(opp.get("chain"), chain),
                        type=_as_str(opp.get("type"), "unknown"),
                        detector=_as_str(opp.get("detector"), det_name),
                    ).inc()

            for opp in all_opps:
                await self._process_opportunity(opp=opp, tx=tx)
        except Exception:
            LOG.exception("message_process_error entry_id=%s tx_hash=%s", entry_id, tx_hash)
        finally:
            try:
                await self.redis.xack(self.stream, self.group, _normalize_stream_id(entry_id))
            except Exception:
                LOG.exception("message_ack_error entry_id=%s stream=%s group=%s", entry_id, self.stream, self.group)

    async def _process_opportunity(self, *, opp: dict[str, Any], tx: Mapping[str, Any]) -> None:
        """Enrich opportunity, hand it to orchestrator, and emit processing metrics."""
        family = _as_str(opp.get("family"), _as_str(tx.get("family"), "unknown"))
        chain = _as_str(opp.get("chain"), _as_str(tx.get("chain"), "unknown"))
        opp_type = _as_str(opp.get("type"), "unknown")
        enriched = dict(opp)
        enriched["detected_at"] = _utc_now_iso()
        enriched["family"] = family
        enriched["chain"] = chain

        started = time.perf_counter()
        outcome = "error"
        try:
            result = await self.orchestrator.handle_opportunity(enriched)
            outcome = _extract_outcome(result)
        except Exception:
            LOG.exception(
                "orchestrator_process_error type=%s family=%s chain=%s detector=%s",
                opp_type,
                family,
                chain,
                _as_str(opp.get("detector"), "unknown"),
            )
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            opportunity_latency_ms.labels(family=family, chain=chain, type=opp_type).observe(latency_ms)
            opportunities_processed_total.labels(
                family=family,
                chain=chain,
                type=opp_type,
                outcome=outcome,
            ).inc()
            LOG.info(
                "Opportunity processed: type=%s outcome=%s latency=%.2fms profit_est=$%.4f family=%s chain=%s detector=%s",
                opp_type,
                outcome,
                latency_ms,
                float(opp.get("expected_profit_usd", 0.0) or 0.0),
                family,
                chain,
                _as_str(opp.get("detector"), "unknown"),
            )

    async def _invoke_detector(self, detector: Any, *, tx: Mapping[str, Any], event: MarketEvent) -> Any:
        """Invoke detector using supported call shapes."""
        if hasattr(detector, "detect"):
            result = detector.detect(tx)
            return await result if inspect.isawaitable(result) else result
        if hasattr(detector, "on_event"):
            result = detector.on_event(event)
            return await result if inspect.isawaitable(result) else result
        return None

    async def _update_queue_depth(self, label_pairs: set[tuple[str, str]]) -> None:
        """Emit approximate queue depth for active family/chain label pairs."""
        if not label_pairs:
            return
        try:
            depth = int(await self.redis.xlen(self.stream))
        except Exception:
            return
        for family, chain in label_pairs:
            opportunity_queue_depth.labels(family=family, chain=chain).set(depth)

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful worker shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)

    def _flush_metrics(self) -> None:
        """Best-effort metrics flush for single and multiprocess deployments."""
        with contextlib.suppress(Exception):
            generate_latest()
        with contextlib.suppress(Exception):
            from prometheus_client import multiprocess

            multiprocess.mark_process_dead(os.getpid())


async def main() -> None:
    """Async entrypoint for the opportunity processor worker."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    orchestrator = await create_orchestrator()
    detectors = _build_detectors(_extract_router(orchestrator))
    processor = OpportunityProcessor(
        redis_url=REDIS_URL,
        stream=REDIS_STREAM,
        group=REDIS_GROUP,
        orchestrator=orchestrator,
        detectors=detectors,
    )
    try:
        await processor.start()
    except KeyboardInterrupt:
        LOG.info("keyboard_interrupt_received; shutting down")
        await processor.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

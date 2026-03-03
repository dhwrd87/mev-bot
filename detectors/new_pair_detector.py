from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from ops import metrics as ops_metrics
from persistence.sqlite_store import PairRecord, SqliteStore

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96f1f60f4d9f60cfc53c280f1b6f7f8f6e8c3"


@dataclass(frozen=True)
class ListingCandidate:
    chain: str
    dex: str
    factory: str
    pool_address: str
    token0: str
    token1: str
    fee_tier: Optional[int]
    event_kind: str
    tx_hash: str = ""
    block_number: Optional[int] = None
    ts: int = 0


def _norm_addr(v: str | None) -> str:
    s = str(v or "").strip().lower()
    if not s:
        return ""
    if s.startswith("0x") and len(s) == 42:
        return s
    if s.startswith("0x"):
        tail = s[2:]
    else:
        tail = s
    if len(tail) > 40:
        tail = tail[-40:]
    return "0x" + tail.rjust(40, "0")


def _topic_to_address(topic: str) -> str:
    s = str(topic or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return _norm_addr("0x" + s[-40:])


def _hex_to_int(v: str | None) -> int:
    s = str(v or "").strip().lower()
    if not s:
        return 0
    return int(s, 16) if s.startswith("0x") else int(s)


def _decode_data_words(data_hex: str) -> list[str]:
    s = str(data_hex or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s:
        return []
    words = [s[i : i + 64] for i in range(0, len(s), 64) if len(s[i : i + 64]) == 64]
    return ["0x" + w for w in words]


class NewPairDetector:
    def __init__(
        self,
        *,
        chain: str,
        dex_deployments: Dict[str, Dict[str, Any]],
        store: Optional[SqliteStore] = None,
    ) -> None:
        self.chain = str(chain or "").strip().lower()
        self.store = store or SqliteStore()
        self.deployments = self._normalize_deployments(dex_deployments)

    @staticmethod
    def _normalize_deployments(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for dex, cfg in (raw or {}).items():
            if not isinstance(cfg, dict):
                continue
            v2 = _norm_addr(cfg.get("factory_v2") or cfg.get("factory") or "")
            v3 = _norm_addr(cfg.get("factory_v3") or cfg.get("v3_factory") or "")
            out[str(dex).strip().lower()] = {"factory_v2": v2, "factory_v3": v3}
        return out

    @classmethod
    def from_registry(cls, *, chain: str, registry: Any, store: Optional[SqliteStore] = None) -> "NewPairDetector":
        deployments: Dict[str, Dict[str, Any]] = {}
        for pack in list(registry.list() if hasattr(registry, "list") else []):
            name = str(pack.name() if hasattr(pack, "name") else "").strip().lower()
            cfg = getattr(pack, "config", {}) or {}
            if "univ2" in name:
                deployments[name] = {"factory_v2": cfg.get("factory") or ""}
            elif "univ3" in name:
                deployments[name] = {"factory_v3": cfg.get("factory") or ""}
        return cls(chain=chain, dex_deployments=deployments, store=store)

    def _match_deployment(self, address: str) -> tuple[str, str]:
        addr = _norm_addr(address)
        for dex, cfg in self.deployments.items():
            if addr and addr == _norm_addr(cfg.get("factory_v2")):
                return dex, "univ2"
            if addr and addr == _norm_addr(cfg.get("factory_v3")):
                return dex, "univ3"
        return "", ""

    def _decode_v2(self, log: Dict[str, Any], *, dex: str, factory: str) -> Optional[ListingCandidate]:
        topics = list(log.get("topics") or [])
        if len(topics) < 3:
            return None
        token0 = _topic_to_address(topics[1])
        token1 = _topic_to_address(topics[2])
        words = _decode_data_words(str(log.get("data") or ""))
        if not words:
            return None
        pair = _topic_to_address(words[0])
        return ListingCandidate(
            chain=self.chain,
            dex=dex,
            factory=factory,
            pool_address=pair,
            token0=token0,
            token1=token1,
            fee_tier=None,
            event_kind="PairCreated",
            tx_hash=str(log.get("transactionHash") or ""),
            block_number=_hex_to_int(log.get("blockNumber")),
            ts=int(time.time()),
        )

    def _decode_v3(self, log: Dict[str, Any], *, dex: str, factory: str) -> Optional[ListingCandidate]:
        topics = list(log.get("topics") or [])
        if len(topics) < 4:
            return None
        token0 = _topic_to_address(topics[1])
        token1 = _topic_to_address(topics[2])
        fee_tier = _hex_to_int(topics[3])
        words = _decode_data_words(str(log.get("data") or ""))
        if len(words) < 2:
            return None
        pool = _topic_to_address(words[1])
        return ListingCandidate(
            chain=self.chain,
            dex=dex,
            factory=factory,
            pool_address=pool,
            token0=token0,
            token1=token1,
            fee_tier=int(fee_tier),
            event_kind="PoolCreated",
            tx_hash=str(log.get("transactionHash") or ""),
            block_number=_hex_to_int(log.get("blockNumber")),
            ts=int(time.time()),
        )

    def process_log(self, log: Dict[str, Any]) -> Optional[ListingCandidate]:
        if not isinstance(log, dict):
            return None
        topics = list(log.get("topics") or [])
        if not topics:
            return None
        topic0 = str(topics[0]).strip().lower()
        address = _norm_addr(str(log.get("address") or ""))
        dex, version = self._match_deployment(address)
        if not dex:
            return None

        cand: Optional[ListingCandidate] = None
        if topic0 == PAIR_CREATED_TOPIC and version == "univ2":
            cand = self._decode_v2(log, dex=dex, factory=address)
        elif topic0 == POOL_CREATED_TOPIC and version == "univ3":
            cand = self._decode_v3(log, dex=dex, factory=address)
        if cand is None:
            return None

        inserted = self.store.insert_pair(
            PairRecord(
                chain=cand.chain,
                dex=cand.dex,
                factory=cand.factory,
                pool_address=cand.pool_address,
                token0=cand.token0,
                token1=cand.token1,
                fee_tier=cand.fee_tier,
                source_event=cand.event_kind,
                discovered_ts=cand.ts,
            )
        )
        if inserted:
            ops_metrics.record_new_pair_seen(dex=cand.dex, chain=cand.chain)
            return cand
        return None

    def process_logs(self, logs: Iterable[Dict[str, Any]]) -> List[ListingCandidate]:
        out: list[ListingCandidate] = []
        for log in logs:
            cand = self.process_log(log)
            if cand is not None:
                out.append(cand)
        return out

    def build_subscriptions(self) -> List[Dict[str, Any]]:
        filters: list[dict[str, Any]] = []
        for _, cfg in sorted(self.deployments.items()):
            v2 = _norm_addr(cfg.get("factory_v2"))
            if v2:
                filters.append(
                    {
                        "address": v2,
                        "topics": [PAIR_CREATED_TOPIC],
                    }
                )
            v3 = _norm_addr(cfg.get("factory_v3"))
            if v3:
                filters.append(
                    {
                        "address": v3,
                        "topics": [POOL_CREATED_TOPIC],
                    }
                )
        return filters

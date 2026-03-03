from __future__ import annotations

from detectors.new_pair_detector import (
    PAIR_CREATED_TOPIC,
    POOL_CREATED_TOPIC,
    NewPairDetector,
)
from persistence.sqlite_store import SqliteStore


def _v2_log(*, factory: str, token0: str, token1: str, pair: str) -> dict:
    return {
        "address": factory,
        "topics": [
            PAIR_CREATED_TOPIC,
            "0x" + ("0" * 24) + token0[2:],
            "0x" + ("0" * 24) + token1[2:],
        ],
        "data": "0x" + ("0" * 24) + pair[2:] + ("0" * 64),  # pair address + trailing word
        "transactionHash": "0xabc",
        "blockNumber": "0x10",
    }


def _v3_log(*, factory: str, token0: str, token1: str, fee: int, pool: str) -> dict:
    return {
        "address": factory,
        "topics": [
            POOL_CREATED_TOPIC,
            "0x" + ("0" * 24) + token0[2:],
            "0x" + ("0" * 24) + token1[2:],
            hex(int(fee)),
        ],
        "data": "0x" + ("0" * 64) + ("0" * 24) + pool[2:],  # tickSpacing + pool
        "transactionHash": "0xdef",
        "blockNumber": "0x20",
    }


def test_new_pair_detector_decodes_v2_and_persists_idempotent(tmp_path):
    store = SqliteStore(str(tmp_path / "discovery.db"))
    det = NewPairDetector(
        chain="sepolia",
        dex_deployments={
            "univ2_default": {"factory_v2": "0x0000000000000000000000000000000000000f01"},
        },
        store=store,
    )
    log = _v2_log(
        factory="0x0000000000000000000000000000000000000f01",
        token0="0x00000000000000000000000000000000000000a1",
        token1="0x00000000000000000000000000000000000000b1",
        pair="0x0000000000000000000000000000000000000p01".replace("p", "a"),
    )
    out1 = det.process_log(log)
    out2 = det.process_log(log)
    assert out1 is not None
    assert out1.event_kind == "PairCreated"
    assert out2 is None  # idempotent insert
    assert store.count_pairs(chain="sepolia", dex="univ2_default") == 1


def test_new_pair_detector_decodes_v3_and_builds_subscriptions(tmp_path):
    store = SqliteStore(str(tmp_path / "discovery.db"))
    det = NewPairDetector(
        chain="base",
        dex_deployments={
            "univ3_default": {"factory_v3": "0x0000000000000000000000000000000000000f03"},
            "univ2_default": {"factory_v2": "0x0000000000000000000000000000000000000f02"},
        },
        store=store,
    )
    subs = det.build_subscriptions()
    assert len(subs) == 2
    assert any(s["topics"][0] == PAIR_CREATED_TOPIC for s in subs)
    assert any(s["topics"][0] == POOL_CREATED_TOPIC for s in subs)

    log = _v3_log(
        factory="0x0000000000000000000000000000000000000f03",
        token0="0x00000000000000000000000000000000000000a1",
        token1="0x00000000000000000000000000000000000000b1",
        fee=3000,
        pool="0x0000000000000000000000000000000000000a33",
    )
    out = det.process_log(log)
    assert out is not None
    assert out.event_kind == "PoolCreated"
    assert int(out.fee_tier or 0) == 3000
    last = store.last_pair(chain="base", dex="univ3_default")
    assert last is not None
    assert last["pool_address"] == "0x0000000000000000000000000000000000000a33"

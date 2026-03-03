import asyncio
import json

from ops.status_data import StatusDataProvider, parse_metrics_text


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _HTTP:
    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    async def get(self, _url):
        if self._i >= len(self._seq):
            raise RuntimeError("no more responses")
        item = self._seq[self._i]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


def test_parse_metrics_text_basic():
    text = """
rpc_latency_seconds_bucket{le="0.05"} 5
rpc_latency_seconds_bucket{le="0.1"} 9
rpc_latency_seconds_bucket{le="+Inf"} 10
trades_sent_total{chain="x"} 11
trades_failed_total{chain="x"} 2
last_trade_timestamp_seconds 1700000000
""".strip()
    out = parse_metrics_text(text)
    assert out["rpc_p95"] == "0.100s"
    assert out["trades_sent_total"] == 11
    assert out["trades_failed_total"] == 2
    assert out["last_trade"] != "—"


def test_snapshot_missing_graceful(tmp_path):
    p = tmp_path / "missing.json"
    d = StatusDataProvider(metrics_scrape_url="", snapshot_path=str(p))
    out = asyncio.run(d.collect(_HTTP([])))
    assert out["active_chain"] == "—"
    assert out["error_counters"] == "—"


def test_stale_uses_last_known_values(tmp_path):
    snap = tmp_path / "health_snapshot.json"
    snap.write_text(
        json.dumps(
            {
                "chain_family": "evm",
                "chain": "sepolia",
                "head_lag": 2,
                "errors_last_10m": 1,
                "last_trade_time": "2026-02-28T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    good = _Resp(
        """
rpc_latency_seconds_bucket{le="0.1"} 1
rpc_latency_seconds_bucket{le="+Inf"} 1
trades_sent_total 5
trades_failed_total 1
""".strip()
    )
    bad = RuntimeError("boom")
    d = StatusDataProvider(metrics_scrape_url="http://metrics", snapshot_path=str(snap))
    http = _HTTP([good, bad])

    first = asyncio.run(d.collect(http))
    second = asyncio.run(d.collect(http))
    assert first["active_chain"] == "evm:sepolia"
    assert first["rpc_p95"] == "0.100s"
    assert second["stale"] is True
    assert "stale" in second["error_counters"]


def test_snapshot_staleness_indicator(tmp_path, monkeypatch):
    snap = tmp_path / "health_snapshot.json"
    snap.write_text(
        json.dumps(
            {
                "ts": 1_000,
                "chain_family": "evm",
                "chain": "sepolia",
                "network": "testnet",
                "tx_sent_10m": 4,
                "tx_failed_10m": 1,
                "opportunities_seen_10m": 9,
                "opportunities_attempted_10m": 3,
                "opportunities_filled_10m": 1,
                "rpc_p95_ms": 10.0,
                "rpc_p99_ms": 20.0,
            }
        ),
        encoding="utf-8",
    )
    d = StatusDataProvider(metrics_scrape_url="", snapshot_path=str(snap), snapshot_stale_after_s=60)
    # Force time to make snapshot stale.
    monkeypatch.setattr("ops.status_data.time.time", lambda: 1_100.0)
    out = asyncio.run(d.collect(_HTTP([])))
    assert out["stale"] is True
    assert str(out["snapshot_status"]).startswith("STALE")
    assert "[STALE]" in str(out["heartbeat_utc"])
    assert out["trades_10m"] == "4/1"
    assert out["opportunities_10m"] == "9/3/1"

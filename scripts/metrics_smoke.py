#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _query(prom_url: str, expr: str) -> float:
    qs = urlencode({"query": expr})
    req = Request(f"{prom_url.rstrip('/')}/api/v1/query?{qs}")
    with urlopen(req, timeout=10) as r:
        body = json.loads(r.read().decode())
    if str(body.get("status")) != "success":
        raise RuntimeError(f"prometheus query failed: {expr}")
    result = body.get("data", {}).get("result", [])
    if not result:
        return 0.0
    try:
        return float(result[0]["value"][1])
    except Exception as e:
        raise RuntimeError(f"unexpected query payload for {expr}: {body}") from e


def _target_up(prom_url: str) -> bool:
    req = Request(f"{prom_url.rstrip('/')}/api/v1/targets")
    with urlopen(req, timeout=10) as r:
        body: dict[str, Any] = json.loads(r.read().decode())
    active = body.get("data", {}).get("activeTargets", [])
    for t in active:
        if str(t.get("labels", {}).get("job")) == "mev-bot" and str(t.get("health")) == "up":
            return True
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke check Prometheus mev-bot exporter/series")
    p.add_argument("--prom-url", default="http://127.0.0.1:9090", help="Prometheus base URL")
    p.add_argument("--wait-seconds", type=int, default=30, help="Wait window for heartbeat delta")
    args = p.parse_args()

    prom_url = str(args.prom_url).strip()
    wait_s = max(5, int(args.wait_seconds))

    try:
        if not _target_up(prom_url):
            raise RuntimeError("target mev-bot is not UP in /api/v1/targets")
        print("OK target mev-bot is UP")

        series_count = _query(prom_url, 'count({__name__=~"mevbot_.*"})')
        if series_count < 10:
            raise RuntimeError(f"expected >=10 mevbot_* series, got {series_count}")
        print(f"OK mevbot series count={series_count:.0f}")

        required_exprs = {
            "heartbeat": 'count(mevbot_heartbeat_ts{family=~".+",chain=~".+",network=~".+",provider=~".+",dex=~".+",strategy=~".+"})',
            "state": "count(mevbot_state)",
            "rpc_latency_buckets": "count(mevbot_rpc_latency_seconds_bucket)",
            "tx_sent": "count(mevbot_tx_sent_total)",
            "tx_failed": "count(mevbot_tx_failed_total)",
            "opps_seen": "count(mevbot_opportunities_seen_total)",
            "opps_attempted": "count(mevbot_opportunities_attempted_total)",
            "opps_filled": "count(mevbot_opportunities_filled_total)",
            "mode_outcomes": "count(mevbot_mode_outcomes_total)",
        }
        for label, expr in required_exprs.items():
            val = _query(prom_url, expr)
            if val <= 0:
                raise RuntimeError(f"missing required metric series: {label} expr={expr}")
            print(f"OK {label} series={val:.0f}")

        heartbeat_now = _query(prom_url, "max(mevbot_heartbeat_ts)")
        print(f"heartbeat_t0={heartbeat_now:.0f}")
        time.sleep(wait_s)
        heartbeat_later = _query(prom_url, "max(mevbot_heartbeat_ts)")
        print(f"heartbeat_t1={heartbeat_later:.0f}")
        if heartbeat_later <= heartbeat_now:
            raise RuntimeError(
                "mevbot_heartbeat_ts did not advance over the wait window. "
                "Ensure mev-bot exporter/runtime monitor loop is running."
            )
        print("OK mevbot_heartbeat_ts advanced")
        return 0
    except Exception as e:
        print(f"METRICS_SMOKE_FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

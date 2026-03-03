#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Dict, List

STREAM = os.getenv("REDIS_STREAM", "mempool:pending:txs")
GROUP = os.getenv("REDIS_GROUP", "mempool")
PRODUCER = os.getenv("MEMPOOL_PRODUCER_CONTAINER", "mev-mempool-producer")
REDIS = os.getenv("REDIS_CONTAINER", "mev-redis")
MAX_AGE_S = int(os.getenv("STREAM_MAX_AGE_S", "600"))


def run(cmd: List[str]) -> str:
    p = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return p.stdout.strip()


def redis_cli(*args: str) -> str:
    return run(["docker", "exec", REDIS, "redis-cli", *args])


def producer_cmdline() -> str:
    return run(["docker", "exec", PRODUCER, "sh", "-lc", "tr '\\0' ' ' </proc/1/cmdline"])


def latest_entry() -> Dict[str, str]:
    raw = redis_cli("XREVRANGE", STREAM, "+", "-", "COUNT", "1")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}

    out: Dict[str, str] = {"id": lines[0]}
    kv = lines[1:]
    for i in range(0, len(kv) - 1, 2):
        out[kv[i]] = kv[i + 1]
    return out


def stream_delta(wait_s: int = 5) -> int:
    before = int(redis_cli("XLEN", STREAM) or "0")
    time.sleep(wait_s)
    after = int(redis_cli("XLEN", STREAM) or "0")
    return after - before


def group_state() -> Dict[str, str]:
    raw = redis_cli("XINFO", "GROUPS", STREAM)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    parsed: Dict[str, str] = {}
    current = None
    i = 0
    while i + 1 < len(lines):
        key, val = lines[i], lines[i + 1]
        if key == "name":
            current = val
        if current == GROUP:
            parsed[key] = val
        i += 2
    return parsed


def main() -> int:
    result: Dict[str, object] = {"ok": False}

    cmd = producer_cmdline()
    result["producer_cmdline"] = cmd
    if "ws_to_redis.py" not in cmd and "WSMempoolMonitor" not in cmd:
        result["error"] = "producer is not running WS->Redis publisher"
        print(json.dumps(result, indent=2))
        return 1

    entry = latest_entry()
    if not entry:
        result["error"] = f"stream '{STREAM}' has no entries"
        print(json.dumps(result, indent=2))
        return 1

    result["latest_entry"] = entry
    missing = [k for k in ("hash", "ts") if k not in entry]
    if missing:
        result["error"] = f"latest entry missing fields: {missing}"
        print(json.dumps(result, indent=2))
        return 1

    age_s = int(time.time()) - int(entry["ts"])
    result["latest_entry_age_s"] = age_s
    if age_s > MAX_AGE_S:
        result["error"] = f"latest entry too old: {age_s}s > {MAX_AGE_S}s"
        print(json.dumps(result, indent=2))
        return 1

    delta = stream_delta(wait_s=5)
    result["stream_delta_5s"] = delta

    grp = group_state()
    result["group"] = grp
    if not grp:
        result["error"] = f"consumer group '{GROUP}' not found on stream"
        print(json.dumps(result, indent=2))
        return 1

    result["ok"] = True
    result["note"] = "delta may be 0 in low-traffic windows; freshness+schema+group validate handoff readiness"
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

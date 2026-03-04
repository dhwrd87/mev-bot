#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _http_json(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from {url}: {e}") from e
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus API error from {url}: {payload}")
    return payload


def _api_get(prom_url: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    q = urllib.parse.urlencode(params or {}, doseq=True)
    sep = "&" if "?" in path else "?"
    url = f"{prom_url.rstrip('/')}{path}{sep}{q}" if q else f"{prom_url.rstrip('/')}{path}"
    return _http_json(url)


def _series_for_metric(prom_url: str, metric_name: str, timeout_s: float) -> list[dict[str, str]]:
    data = _api_get(
        prom_url,
        "/api/v1/series",
        {"match[]": metric_name, "start": str(int(time.time()) - 3600), "end": str(int(time.time()))},
    )
    out = data.get("data", [])
    if not isinstance(out, list):
        return []
    series: list[dict[str, str]] = []
    for item in out:
        if isinstance(item, dict):
            series.append({str(k): str(v) for k, v in item.items()})
    return series


def build_contract(prom_url: str, include_regex: str, sample_limit: int, timeout_s: float) -> dict[str, Any]:
    name_re = re.compile(include_regex)
    names_payload = _api_get(prom_url, "/api/v1/label/__name__/values")
    names = [str(n) for n in names_payload.get("data", []) if isinstance(n, str)]

    metrics: dict[str, Any] = {}
    selected = [n for n in names if name_re.search(n)]
    for metric in sorted(selected):
        try:
            series = _series_for_metric(prom_url, metric, timeout_s=timeout_s)
        except Exception as e:
            metrics[metric] = {
                "label_keys": [],
                "samples": [],
                "error": str(e),
            }
            continue
        keys: set[str] = set()
        for s in series:
            keys.update(k for k in s.keys() if k != "__name__")
        samples = []
        for s in series[: max(1, sample_limit)]:
            samples.append({k: v for k, v in s.items() if k != "__name__"})
        metrics[metric] = {
            "label_keys": sorted(keys),
            "samples": samples,
        }

    return {
        "generated_at": int(time.time()),
        "prometheus_url": prom_url,
        "include_regex": include_regex,
        "metric_count": len(metrics),
        "metrics": metrics,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Prometheus metrics contract (metric -> label keys + sample series).")
    ap.add_argument("--prom-url", default="http://127.0.0.1:9090", help="Prometheus base URL")
    ap.add_argument(
        "--include-regex",
        default=r"^(mevbot_.*|up|scrape_.*)$",
        help="Regex for metric names to include",
    )
    ap.add_argument("--sample-limit", type=int, default=3, help="How many sample series per metric")
    ap.add_argument("--timeout-s", type=float, default=10.0, help="HTTP timeout")
    ap.add_argument(
        "--out",
        default="artifacts/prom_contract.json",
        help="Output JSON file path (use '-' for stdout only)",
    )
    args = ap.parse_args()

    try:
        contract = build_contract(
            prom_url=args.prom_url,
            include_regex=args.include_regex,
            sample_limit=max(1, args.sample_limit),
            timeout_s=max(1.0, args.timeout_s),
        )
    except Exception as e:
        print(f"ERROR: failed building contract: {e}", file=sys.stderr)
        return 2

    out = json.dumps(contract, indent=2, sort_keys=True)
    if args.out == "-":
        print(out)
    else:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(out + "\n", encoding="utf-8")
        print(f"Wrote {p} (metrics={contract.get('metric_count', 0)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

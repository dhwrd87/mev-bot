#!/usr/bin/env bash
set -euo pipefail

PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

echo "Verifying Grafana dashboard PromQL via Prometheus API: ${PROM_URL}"

python3 - "$PROM_URL" <<'PY'
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

prom_url = sys.argv[1].rstrip("/")

dashboard_roots = [
    pathlib.Path("grafana/dashboards/00-operator"),
    pathlib.Path("grafana/dashboards/10-execution"),
    pathlib.Path("grafana/dashboards/20-strategy"),
    pathlib.Path("grafana/dashboards/90-infra"),
]

def _iter_exprs(doc):
    panels = doc.get("panels", []) if isinstance(doc, dict) else []
    for panel in panels:
        for t in panel.get("targets", []) or []:
            expr = t.get("expr")
            if isinstance(expr, str) and expr.strip():
                yield panel.get("title", "unknown"), expr

def _normalize_expr(expr: str) -> str:
    out = expr
    # Grafana template vars -> broad regex or safe interval defaults.
    replacements = {
        "$family": ".*",
        "$chain": ".*",
        "$network": ".*",
        "$endpoint": ".*",
        "$dex": ".*",
        "$strategy": ".*",
        "$provider": ".*",
        "$__all": ".*",
        "$__interval": "1m",
        "$__rate_interval": "5m",
    }
    for k, v in replacements.items():
        out = out.replace(k, v)
        out = out.replace("${" + k[1:] + "}", v)
    return out

def _query(expr: str):
    qs = urllib.parse.urlencode({"query": expr})
    url = f"{prom_url}/api/v1/query?{qs}"
    with urllib.request.urlopen(url, timeout=8) as r:
        payload = json.loads(r.read().decode("utf-8", errors="ignore"))
    return payload

checked = 0
for root in dashboard_roots:
    if not root.exists():
        continue
    for path in sorted(root.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for panel_title, expr in _iter_exprs(doc):
            checked += 1
            resolved = _normalize_expr(expr)
            payload = _query(resolved)
            status = payload.get("status")
            if status != "success":
                err_t = payload.get("errorType", "unknown")
                err = payload.get("error", "unknown")
                raise SystemExit(
                    f"PromQL validation failed: file={path} panel={panel_title} expr={expr!r} resolved={resolved!r} errorType={err_t} error={err}"
                )

print(f"OK: validated {checked} dashboard queries (parse/API success)")
PY

echo "verify_dashboards_promql.sh passed"

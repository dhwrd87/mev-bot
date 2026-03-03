from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Alert Webhook Relay")
log = logging.getLogger("alert-webhook-relay")


def _format_alerts(payload: Dict[str, Any]) -> str:
    status = str(payload.get("status", "unknown")).upper()
    alerts: List[Dict[str, Any]] = list(payload.get("alerts") or [])
    head = f"[{status}] {len(alerts)} alert(s)"
    lines = [head]
    for a in alerts[:8]:
        labels = a.get("labels") or {}
        annotations = a.get("annotations") or {}
        name = labels.get("alertname", "unknown")
        severity = labels.get("severity", "unknown")
        service = labels.get("service", "unknown")
        summary = annotations.get("summary") or annotations.get("description") or ""
        lines.append(f"- {name} severity={severity} service={service}")
        if summary:
            lines.append(f"  {summary}")
    if len(alerts) > 8:
        lines.append(f"... and {len(alerts) - 8} more")
    return "\n".join(lines)[:1900]


@app.post("/webhook")
async def webhook(req: Request):
    payload = await req.json()
    target = os.getenv("ALERT_WEBHOOK", "").strip()
    if not target:
        log.warning("ALERT_WEBHOOK is empty; dropping alert payload")
        return JSONResponse({"ok": False, "dropped": True, "reason": "missing ALERT_WEBHOOK"})

    content = _format_alerts(payload)
    try:
        resp = requests.post(target, json={"content": content}, timeout=8)
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("discord webhook returned %s body=%s", resp.status_code, resp.text[:300])
        return JSONResponse({"ok": ok, "status_code": resp.status_code})
    except Exception as e:
        log.warning("relay post failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


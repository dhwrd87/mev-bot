from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Operator Alert Router")
log = logging.getLogger("ops.alert_router")

_SEEN: Dict[str, float] = {}
_DEDUP_TTL_S = 300.0

_RUNBOOK_HINTS: Dict[str, str] = {
    "TargetDown": "Check docker compose status and service logs for mev-bot.",
    "MempoolProducerDisconnected": "Check mempool-producer logs and WS endpoint connectivity.",
    "ConsumerLagHigh": "Inspect Redis stream/group lag and mempool-consumer throughput.",
    "ConsumerErrorRateHigh": "Review mempool-consumer RPC errors, 429 ratios, and circuit breaker state.",
    "StreamNotGrowing": "Verify producer websocket connectivity and upstream mempool flow.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_fingerprint(fp: str, now: float | None = None) -> bool:
    t = time.time() if now is None else float(now)
    # cleanup expired entries
    expired = [k for k, exp in _SEEN.items() if exp <= t]
    for k in expired:
        _SEEN.pop(k, None)
    if not fp:
        return False
    exp = _SEEN.get(fp)
    if exp and exp > t:
        return True
    _SEEN[fp] = t + _DEDUP_TTL_S
    return False


def _target_channel_id() -> str:
    return (
        str(os.getenv("DISCORD_OPERATOR_ALERTS_CHANNEL_ID", "")).strip()
        or str(os.getenv("DISCORD_OPERATOR_AUDIT_CHANNEL_ID", "")).strip()
    )


def _alert_embed(alert: Dict[str, Any], payload_status: str) -> Dict[str, Any]:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    alertname = str(labels.get("alertname", "unknown"))
    severity = str(labels.get("severity", "unknown"))
    status = str(alert.get("status", payload_status or "firing")).lower()
    summary = str(annotations.get("summary") or annotations.get("description") or "No summary")
    fingerprint = str(alert.get("fingerprint", ""))
    service = str(labels.get("service", "unknown"))
    hint = _RUNBOOK_HINTS.get(alertname, "Check relevant service logs and dashboard panels.")
    color = 0xE74C3C if status == "firing" else 0x2ECC71
    return {
        "title": f"[{status.upper()}] {alertname}",
        "description": summary[:1600],
        "color": color,
        "fields": [
            {"name": "Severity", "value": severity, "inline": True},
            {"name": "Service", "value": service, "inline": True},
            {"name": "Fingerprint", "value": (fingerprint or "—"), "inline": False},
            {"name": "Runbook Hint", "value": hint, "inline": False},
        ],
        "footer": {"text": f"alert-router { _now_iso() }"},
    }


async def _send_discord_embed(channel_id: str, embed: Dict[str, Any]) -> tuple[bool, str]:
    token = str(os.getenv("DISCORD_OPERATOR_TOKEN", "")).strip()
    if not token:
        return False, "missing DISCORD_OPERATOR_TOKEN"
    if not channel_id:
        return False, "missing DISCORD_OPERATOR_ALERTS_CHANNEL_ID or DISCORD_OPERATOR_AUDIT_CHANNEL_ID"
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(url, headers=headers, json={"embeds": [embed]})
    if 200 <= resp.status_code < 300:
        return True, "ok"
    return False, f"http_{resp.status_code}:{resp.text[:200]}"


@app.post("/alertmanager")
async def alertmanager_webhook(req: Request):
    payload = await req.json()
    alerts: List[Dict[str, Any]] = list(payload.get("alerts") or [])
    status = str(payload.get("status", "firing"))
    channel_id = _target_channel_id()

    sent = 0
    deduped = 0
    errors: List[str] = []

    for a in alerts:
        fp = str(a.get("fingerprint", ""))
        if _dedupe_fingerprint(fp):
            deduped += 1
            continue
        embed = _alert_embed(a, status)
        ok, msg = await _send_discord_embed(channel_id, embed)
        if ok:
            sent += 1
        else:
            errors.append(msg)

    out = {
        "ok": len(errors) == 0,
        "alerts": len(alerts),
        "sent": sent,
        "deduped": deduped,
        "errors": errors[:5],
    }
    code = 200 if len(errors) == 0 else 502
    if errors:
        log.warning("alert router partial failure: %s", out)
    return JSONResponse(out, status_code=code)


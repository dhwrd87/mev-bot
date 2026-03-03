from ops.alert_router import _dedupe_fingerprint, _alert_embed


def test_dedupe_fingerprint_ttl():
    fp = "abc123"
    assert _dedupe_fingerprint(fp, now=100.0) is False
    assert _dedupe_fingerprint(fp, now=120.0) is True
    assert _dedupe_fingerprint(fp, now=401.0) is False


def test_alert_embed_includes_runbook_hint():
    alert = {
        "status": "firing",
        "fingerprint": "fp1",
        "labels": {"alertname": "TargetDown", "severity": "critical", "service": "mev-bot"},
        "annotations": {"summary": "target is down"},
    }
    em = _alert_embed(alert, "firing")
    assert em["title"].startswith("[FIRING] TargetDown")
    fields = {f["name"]: f["value"] for f in em["fields"]}
    assert "Runbook Hint" in fields
    assert "logs" in fields["Runbook Hint"].lower() or "check" in fields["Runbook Hint"].lower()


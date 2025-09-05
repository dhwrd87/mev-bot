import pytest, respx, httpx, asyncio
from bot.telemetry.alerts import AlertManager, AlertCfg

pytestmark = pytest.mark.asyncio

@respx.mock
async def test_alert_cooldown_and_send():
    route = respx.post("https://discord").mock(return_value=httpx.Response(204))
    am = AlertManager(AlertCfg(webhook="https://discord", enabled=True, default_cooldown_s=60))
    await am.send("info","t","m","k",{"a":1}, cooldown_s=0)
    await am.send("info","t","m","k",{"a":1}, cooldown_s=9999)  # throttled
    assert route.call_count == 1

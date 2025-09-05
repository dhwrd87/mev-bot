from __future__ import annotations
import asyncio, os, time
from dataclasses import dataclass
from typing import Dict, Optional, Any
import httpx

@dataclass
class AlertCfg:
    webhook: str
    service: str = "mev-bot"
    enabled: bool = True
    default_cooldown_s: int = 60

class AlertManager:
    def __init__(self, cfg: AlertCfg):
        self.cfg = cfg
        self._last_sent: Dict[str, float] = {}
        self._client = httpx.AsyncClient(timeout=5.0)

    def _should_send(self, key: str, cooldown_s: Optional[int]) -> bool:
        if not self.cfg.enabled: return False
        now = time.time()
        cd = cooldown_s if cooldown_s is not None else self.cfg.default_cooldown_s
        last = self._last_sent.get(key, 0.0)
        if now - last >= cd:
            self._last_sent[key] = now
            return True
        return False

    async def _post(self, payload: Dict[str, Any]) -> None:
        try:
            await self._client.post(self.cfg.webhook, json=payload)
        except Exception:
            # don’t crash trading on alert errors
            pass

    async def send(self, level: str, title: str, message: str, key: str,
                   fields: Optional[Dict[str, Any]]=None, cooldown_s: Optional[int]=None):
        """
        key: dedup key (e.g. 'sim_failed|pair|route'); cooldown_s: per-key throttle
        """
        if not self._should_send(key, cooldown_s): return
        color_map = {"critical": 0xFF0000, "warning": 0xFFAA00, "info": 0x3399FF, "success": 0x00CC66}
        embed = {
            "title": f"{title}",
            "description": message,
            "color": color_map.get(level, 0x808080),
            "fields": [{"name": k, "value": str(v), "inline": True} for k,v in (fields or {}).items()],
        }
        payload = {
            "username": self.cfg.service,
            "embeds": [embed]
        }
        await self._post(payload)

    async def close(self):
        await self._client.aclose()

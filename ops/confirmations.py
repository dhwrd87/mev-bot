from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Confirmation:
    token: str
    action: str
    args: Dict[str, Any]
    actor_id: int
    expires_at: float


class ConfirmationStore:
    def __init__(self, ttl_s: float = 60.0) -> None:
        self.ttl_s = float(ttl_s)
        self._items: Dict[str, Confirmation] = {}

    def create(self, *, action: str, args: Dict[str, Any], actor_id: int, now: Optional[float] = None) -> Confirmation:
        t = time.monotonic() if now is None else float(now)
        token = secrets.token_hex(3).upper()
        item = Confirmation(
            token=token,
            action=str(action),
            args=dict(args),
            actor_id=int(actor_id),
            expires_at=t + self.ttl_s,
        )
        self._items[token] = item
        self._gc(now=t)
        return item

    def consume(self, *, token: str, actor_id: int, now: Optional[float] = None) -> tuple[bool, Optional[Confirmation], str]:
        t = time.monotonic() if now is None else float(now)
        self._gc(now=t)
        item = self._items.get(token)
        if not item:
            return False, None, "invalid_or_expired"
        if int(actor_id) != item.actor_id:
            return False, None, "actor_mismatch"
        if t > item.expires_at:
            self._items.pop(token, None)
            return False, None, "expired"
        self._items.pop(token, None)
        return True, item, "ok"

    def _gc(self, now: Optional[float] = None) -> None:
        t = time.monotonic() if now is None else float(now)
        expired = [k for k, v in self._items.items() if v.expires_at < t]
        for k in expired:
            self._items.pop(k, None)


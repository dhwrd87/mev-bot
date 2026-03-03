from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Set


def parse_id_csv(raw: str | None) -> Set[int]:
    out: Set[int] = set()
    if not raw:
        return out
    for part in str(raw).split(","):
        p = part.strip()
        if not p:
            continue
        out.add(int(p))
    return out


@dataclass
class CommandGuard:
    allowed_user_ids: Set[int] = field(default_factory=set)
    allowed_role_ids: Set[int] = field(default_factory=set)
    _open_mode_warned: bool = False

    def authorize(self, *, user_id: int, role_ids: Iterable[int]) -> tuple[bool, str]:
        if self.allowed_user_ids:
            if user_id in self.allowed_user_ids:
                return True, "ok_user"
            return False, "unauthorized_user"
        if self.allowed_role_ids:
            if any(r in self.allowed_role_ids for r in role_ids):
                return True, "ok_role"
            return False, "unauthorized_role"
        if not self._open_mode_warned:
            self._open_mode_warned = True
        return True, "open_mode"


class UserRateLimiter:
    def __init__(self, *, limit: int = 3, window_s: float = 10.0) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._events: Dict[int, deque[float]] = {}

    def allow(self, user_id: int, *, now: Optional[float] = None) -> tuple[bool, float]:
        t = time.monotonic() if now is None else float(now)
        q = self._events.setdefault(int(user_id), deque())
        cutoff = t - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) < self.limit:
            q.append(t)
            return True, 0.0
        retry_after = max(0.0, self.window_s - (t - q[0]))
        return False, retry_after


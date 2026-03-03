from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from bot.core.operator_control import get_operator_state
from ops import metrics as ops_metrics


@dataclass(frozen=True)
class RiskDecision:
    classification: str  # ALLOW | WATCH | DENY
    reasons: List[str]
    token: str
    pool: str


def _norm(v: str | None) -> str:
    return str(v or "").strip().lower()


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "on"}


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


class RiskFirewall:
    def __init__(self, *, chain: str, operator_state_path: Optional[str] = None) -> None:
        self.chain = _norm(chain) or "unknown"
        self.operator_state_path = operator_state_path
        self.enabled = _as_bool(os.getenv("RISK_FIREWALL_ENABLED", "1"))
        self.tax_bps_deny = _as_int(os.getenv("RISK_FIREWALL_TAX_BPS_DENY", "1000"), default=1000)
        self.tiny_amount = max(1, _as_int(os.getenv("RISK_FIREWALL_TINY_AMOUNT", "100"), default=100))

    def _overrides(self) -> Dict[str, List[str]]:
        st = get_operator_state(path=self.operator_state_path)
        raw = st.get("risk_overrides") if isinstance(st.get("risk_overrides"), dict) else {}

        def _lst(k: str) -> list[str]:
            vals = raw.get(k, [])
            if isinstance(vals, str):
                vals = [x.strip() for x in vals.split(",")]
            if not isinstance(vals, (list, tuple, set)):
                return []
            return sorted({_norm(x) for x in vals if _norm(x)})

        return {
            "allow_tokens": _lst("allow_tokens"),
            "deny_tokens": _lst("deny_tokens"),
            "watch_tokens": _lst("watch_tokens"),
            "allow_pools": _lst("allow_pools"),
            "deny_pools": _lst("deny_pools"),
            "watch_pools": _lst("watch_pools"),
        }

    def static_checks(self, *, token: str, pool: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        md = metadata or {}
        reasons: list[str] = []
        if _as_bool(md.get("is_proxy")):
            reasons.append("proxy_contract")
        if _as_bool(md.get("blacklist_enabled")):
            reasons.append("blacklist_enabled")
        if _as_bool(md.get("owner_can_block")):
            reasons.append("owner_can_block")
        if _as_bool(md.get("owner_can_mint")):
            reasons.append("owner_can_mint")
        if not _as_bool(md.get("owner_renounced", True)):
            reasons.append("owner_not_renounced")
        buy_tax = _as_int(md.get("buy_tax_bps"), default=0)
        sell_tax = _as_int(md.get("sell_tax_bps"), default=0)
        if max(buy_tax, sell_tax) >= self.tax_bps_deny:
            reasons.append("high_tax_tokenomics")
        return reasons

    @staticmethod
    def _run_check(fn: Optional[Callable[[], tuple[bool, str]]]) -> tuple[bool, str]:
        if fn is None:
            return False, "dynamic_check_unavailable"
        try:
            ok, reason = fn()
            return bool(ok), str(reason or ("ok" if ok else "failed"))
        except Exception as e:
            return False, f"exception:{e}"

    def evaluate(
        self,
        *,
        token: str,
        pool: str,
        metadata: Optional[Dict[str, Any]] = None,
        simulate_buy: Optional[Callable[[], tuple[bool, str]]] = None,
        simulate_sell: Optional[Callable[[], tuple[bool, str]]] = None,
    ) -> RiskDecision:
        token_n = _norm(token)
        pool_n = _norm(pool)
        ov = self._overrides()

        # manual deny first, manual allow can still be explicit but deny should win for safety
        if token_n in ov["deny_tokens"] or pool_n in ov["deny_pools"]:
            d = RiskDecision("DENY", ["manual_override_deny"], token_n, pool_n)
            ops_metrics.record_risk_deny(chain=self.chain)
            return d
        if token_n in ov["allow_tokens"] or pool_n in ov["allow_pools"]:
            d = RiskDecision("ALLOW", ["manual_override_allow"], token_n, pool_n)
            ops_metrics.record_risk_allow(chain=self.chain)
            return d

        if not self.enabled:
            d = RiskDecision("ALLOW", ["firewall_disabled"], token_n, pool_n)
            ops_metrics.record_risk_allow(chain=self.chain)
            return d

        reasons = self.static_checks(token=token_n, pool=pool_n, metadata=metadata)
        buy_ok, buy_reason = self._run_check(simulate_buy)
        sell_ok, sell_reason = self._run_check(simulate_sell)

        if not sell_ok:
            reason = f"sell_sim_failed:{sell_reason}"
            ops_metrics.record_sell_sim_fail(chain=self.chain, reason=reason)
            d = RiskDecision("DENY", reasons + [reason], token_n, pool_n)
            ops_metrics.record_risk_deny(chain=self.chain)
            return d

        if not buy_ok:
            d = RiskDecision("WATCH", reasons + [f"buy_sim_failed:{buy_reason}"], token_n, pool_n)
            ops_metrics.record_risk_watch(chain=self.chain)
            return d

        if token_n in ov["watch_tokens"] or pool_n in ov["watch_pools"] or reasons:
            d = RiskDecision("WATCH", reasons or ["manual_override_watch"], token_n, pool_n)
            ops_metrics.record_risk_watch(chain=self.chain)
            return d

        d = RiskDecision("ALLOW", ["dynamic_checks_ok"], token_n, pool_n)
        ops_metrics.record_risk_allow(chain=self.chain)
        return d

    def should_exclude(
        self,
        *,
        token: str,
        pool: str,
        metadata: Optional[Dict[str, Any]] = None,
        simulate_buy: Optional[Callable[[], tuple[bool, str]]] = None,
        simulate_sell: Optional[Callable[[], tuple[bool, str]]] = None,
    ) -> tuple[bool, RiskDecision]:
        d = self.evaluate(
            token=token,
            pool=pool,
            metadata=metadata,
            simulate_buy=simulate_buy,
            simulate_sell=simulate_sell,
        )
        return d.classification == "DENY", d

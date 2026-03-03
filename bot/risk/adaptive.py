from dataclasses import dataclass
from typing import Tuple, Dict
from bot.core.telemetry import risk_blocks_total, risk_state_gauge

@dataclass
class RiskConfig:
    capital_usd: float = 10000.0
    max_position_size_pct: float = 5.0      # % of capital
    max_daily_loss_usd: float = 1000.0
    max_consecutive_losses: int = 5

class AdaptiveRiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.daily_pnl = 0.0
        self.consecutive_losses = 0

    def _emit_state(self):
        risk_state_gauge.labels("daily_pnl_usd").set(self.daily_pnl)
        # drawdown vs capital
        dd = min(0.0, self.daily_pnl) / max(1.0, self.cfg.capital_usd)
        risk_state_gauge.labels("drawdown_pct").set(abs(dd) * 100.0)
        risk_state_gauge.labels("consecutive_losses").set(self.consecutive_losses)

    def position_cap(self, size_usd: float) -> float:
        cap = self.cfg.capital_usd * (self.cfg.max_position_size_pct / 100.0)
        return min(size_usd, cap)

    def should_execute(self, opp: Dict) -> Tuple[bool, str]:
        # Gates: daily loss, consecutive losses, position size
        if self.daily_pnl <= -abs(self.cfg.max_daily_loss_usd):
            risk_blocks_total.labels("daily_drawdown").inc()
            self._emit_state()
            return False, "daily_drawdown"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            risk_blocks_total.labels("consecutive_losses").inc()
            self._emit_state()
            return False, "consecutive_losses"
        if opp.get("size_usd", 0) > self.position_cap(opp.get("size_usd", 0)):
            risk_blocks_total.labels("position_cap").inc()
            self._emit_state()
            return False, "position_cap"
        self._emit_state()
        return True, "ok"

    def record_result(self, pnl_usd: float):
        self.daily_pnl += pnl_usd
        self.consecutive_losses = 0 if pnl_usd > 0 else self.consecutive_losses + 1
        self._emit_state()

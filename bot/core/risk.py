class AdaptiveRiskManager:
    def __init__(self):
        self.performance_history = deque(maxlen=1000)
        self.current_exposure = 0
        self.daily_pnl = 0

    def calculate_position_size(self, opportunity: Opportunity) -> Wei:
        """Kelly Criterion-based position sizing"""
        win_rate = self.calculate_win_rate()
        avg_win = self.calculate_avg_win()
        avg_loss = self.calculate_avg_loss()

        # Kelly formula: f = (p * b - q) / b
        # where p = win_rate, q = 1-p, b = avg_win/avg_loss
        if avg_loss > 0:
            b = avg_win / avg_loss
            kelly_fraction = (win_rate * b - (1 - win_rate)) / b

            # Apply safety factor (never bet full Kelly)
            safe_fraction = kelly_fraction * 0.25

            # Apply constraints
            max_position = min(
                self.available_capital * safe_fraction,
                self.max_position_size,
                opportunity.liquidity * 0.1  # Max 10% of pool liquidity
            )

            return max_position

        return self.min_position_size

    def should_execute(self, trade: Trade) -> bool:
        """Multi-factor go/no-go decision"""
        checks = {
            'daily_loss_limit': self.daily_pnl > -self.max_daily_loss,
            'consecutive_losses': self.consecutive_losses < 5,
            'gas_reasonable': trade.estimated_gas < trade.expected_profit * 0.3,
            'liquidity_sufficient': trade.pool_liquidity > trade.size * 10,
            'risk_score_acceptable': trade.risk_score < 0.7
        }

        return all(checks.values())

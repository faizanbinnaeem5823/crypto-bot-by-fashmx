import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

class PositionSizer:
    """Kelly-inspired position sizing with conservative half-Kelly."""

    def __init__(self, config: dict):
        self.config = config

    def fixed_fractional(self, portfolio: Decimal, risk_pct: float) -> Decimal:
        return portfolio * Decimal(str(risk_pct)) / Decimal("100")

    def kelly_size(self, portfolio: Decimal, win_rate: float, avg_win: Decimal, avg_loss: Decimal) -> Decimal:
        if avg_loss == 0:
            return Decimal("0")
        win_rate_d = Decimal(str(win_rate))
        avg_ratio = avg_win / avg_loss
        kelly = win_rate_d - ((Decimal("1") - win_rate_d) / avg_ratio)
        half_kelly = kelly / Decimal("2")
        if half_kelly <= 0:
            return Decimal("0")
        return portfolio * half_kelly

import logging
from decimal import Decimal
from typing import Dict, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class MetricsReporter:
    """Reports bot performance metrics."""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id

    def calculate_sharpe(self, returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns or len(returns) < 2:
            return 0.0
        import numpy as np
        excess = [r - risk_free_rate for r in returns]
        return np.mean(excess) / (np.std(excess) + 1e-10) * (252 ** 0.5)

    def calculate_max_drawdown(self, equity_curve: List[float]) -> float:
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def calculate_win_rate(self, trades: List[Dict]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        return wins / len(trades) * 100

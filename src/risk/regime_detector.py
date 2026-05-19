import logging
import pandas as pd

logger = logging.getLogger(__name__)

class RegimeDetector:
    """Detects market regime: trending, ranging, volatile."""

    def detect(self, candles: pd.DataFrame) -> str:
        if len(candles) < 20:
            return "unknown"
        returns = candles['close'].pct_change().dropna()
        volatility = returns.std() * (252 ** 0.5)
        if volatility > 0.8:
            return "volatile"
        elif volatility > 0.3:
            return "trending"
        return "ranging"

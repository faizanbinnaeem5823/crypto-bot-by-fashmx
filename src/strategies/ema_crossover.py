"""
EMA Crossover Strategy for CryptoBot.

Generates buy signals when the fast EMA crosses above the slow EMA.
Generates sell signals when the fast EMA crosses below the slow EMA.

This is a classic trend-following strategy that works well in directional markets.
For crypto (BTC/USD, ETH/USD), EMA crossovers on 1h-4h timeframes historically
provide a good balance of signal quality and trade frequency.

Configurable parameters:
    - fast: Fast EMA period (default: 9)
    - slow: Slow EMA period (default: 21)
    - cooldown_periods: Minimum bars between trades (default: 0)
    - max_position_bars: Auto-exit after N bars (default: None / disabled)
"""

from typing import Dict, Any, Tuple

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):
    """
    EMA Crossover strategy.

    BUY:  Fast EMA crosses above Slow EMA (bullish crossover)
    SELL: Fast EMA crosses below Slow EMA (bearish crossover)

    Parameters:
        fast (int): Fast EMA period (default 9)
        slow (int): Slow EMA period (default 21)
    """

    NAME = "EMA_Cross"

    def __init__(self, params: Dict[str, Any] = None):
        """Initialize with optional parameter overrides."""
        super().__init__(params)
        self.fast = self._params["fast"]
        self.slow = self._params["slow"]

    # ------------------------------------------------------------------
    #  Parameter defaults & ranges
    # ------------------------------------------------------------------

    @classmethod
    def default_parameters(cls) -> Dict[str, Any]:
        """Return default parameter values."""
        defaults = super().default_parameters()
        defaults.update({
            "fast": 9,
            "slow": 21,
        })
        return defaults

    @classmethod
    def parameter_ranges(cls) -> Dict[str, Tuple[int, int, int]]:
        """Return optimization ranges for walk-forward analysis."""
        return {
            "fast": (5, 20, 1),
            "slow": (15, 50, 1),
        }

    # ------------------------------------------------------------------
    #  Core signal generation
    # ------------------------------------------------------------------

    def get_name(self) -> str:
        """Return strategy name."""
        return self.NAME

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate entry/exit signals from OHLCV data.

        Args:
            df: DataFrame with ['open','high','low','close','volume']
                and DatetimeIndex.

        Returns:
            DataFrame with columns:
                - entry (bool): True on bullish crossover
                - exit  (bool): True on bearish crossover
                - fast_ema (float): Fast EMA values
                - slow_ema (float): Slow EMA values
        """
        self.validate_dataframe(df)
        close = df["close"]

        # Calculate EMAs
        fast_ema = close.ewm(span=self.fast, adjust=False).mean()
        slow_ema = close.ewm(span=self.slow, adjust=False).mean()

        # Crossover detection
        # Entry: fast crosses ABOVE slow (was <=, now >)
        entries = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
        # Exit:  fast crosses BELOW slow (was >=, now <)
        exits = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

        # Apply risk filters (cooldown, max position time)
        entries, exits = self.apply_risk_filters(entries, exits, df.index)

        return pd.DataFrame({
            "entry": entries,
            "exit": exits,
            "fast_ema": fast_ema,
            "slow_ema": slow_ema,
        }, index=df.index)

    # ------------------------------------------------------------------
    #  Live trading helper
    # ------------------------------------------------------------------

    def calculate_indicators(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Calculate indicator values for the latest bar.
        Used by live trading to check conditions in real-time.

        Returns dict with current indicator state for logging/display.
        """
        close = df["close"]
        fast_ema = close.ewm(span=self.fast, adjust=False).mean()
        slow_ema = close.ewm(span=self.slow, adjust=False).mean()

        latest = {
            "fast_ema": fast_ema.iloc[-1],
            "slow_ema": slow_ema.iloc[-1],
            "fast_above_slow": fast_ema.iloc[-1] > slow_ema.iloc[-1],
            "trend": "UP" if fast_ema.iloc[-1] > slow_ema.iloc[-1] else "DOWN",
        }
        return latest

    def check_signal(self, df: pd.DataFrame) -> str:
        """
        Check if a signal is triggered on the latest bar.

        Returns:
            'BUY'  - bullish crossover detected
            'SELL' - bearish crossover detected
            'HOLD' - no signal
        """
        signals = self.generate_signals(df)
        last_entry = signals["entry"].iloc[-1]
        last_exit = signals["exit"].iloc[-1]

        if last_entry:
            return "BUY"
        elif last_exit:
            return "SELL"
        return "HOLD"

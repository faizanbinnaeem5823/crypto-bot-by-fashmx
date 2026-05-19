"""
RSI + MACD Combo Strategy for CryptoBot.

A multi-indicator strategy that combines RSI (momentum) with MACD (trend)
for higher-confidence trade signals. Designed to reduce false entries in
choppy crypto markets.

Signal Logic:
    BUY:  RSI < oversold_threshold (default 40) AND MACD line crosses above signal
    SELL: RSI > overbought_threshold (default 60) AND MACD line crosses below signal

This means we only buy pullbacks in an emerging uptrend (RSI oversold + MACD
bullish cross) and only sell rallies in an emerging downtrend (RSI overbought
+ MACD bearish cross).

Configurable parameters:
    - rsi_period: RSI lookback (default: 14)
    - oversold: RSI oversold threshold for buy (default: 40)
    - overbought: RSI overbought threshold for sell (default: 60)
    - macd_fast: MACD fast EMA (default: 12)
    - macd_slow: MACD slow EMA (default: 26)
    - macd_signal: MACD signal EMA (default: 9)
"""

from typing import Dict, Any, Tuple

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy


class RSIMACDStrategy(BaseStrategy):
    """
    RSI + MACD Combo strategy.

    BUY:  RSI < oversold (default 40) AND MACD line crosses above Signal line
    SELL: RSI > overbought (default 60) AND MACD line crosses below Signal line

    Parameters:
        rsi_period   (int): RSI lookback (default 14)
        oversold     (int): RSI buy threshold (default 40)
        overbought   (int): RSI sell threshold (default 60)
        macd_fast    (int): MACD fast EMA (default 12)
        macd_slow    (int): MACD slow EMA (default 26)
        macd_signal  (int): MACD signal line EMA (default 9)
    """

    NAME = "RSI_MACD"

    def __init__(self, params: Dict[str, Any] = None):
        """Initialize with optional parameter overrides."""
        super().__init__(params)
        self.rsi_period = self._params["rsi_period"]
        self.oversold = self._params["oversold"]
        self.overbought = self._params["overbought"]
        self.macd_fast = self._params["macd_fast"]
        self.macd_slow = self._params["macd_slow"]
        self.macd_signal = self._params["macd_signal"]

    # ------------------------------------------------------------------
    #  Parameter defaults & ranges
    # ------------------------------------------------------------------

    @classmethod
    def default_parameters(cls) -> Dict[str, Any]:
        """Return default parameter values."""
        defaults = super().default_parameters()
        defaults.update({
            "rsi_period": 14,
            "oversold": 40,
            "overbought": 60,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
        })
        return defaults

    @classmethod
    def parameter_ranges(cls) -> Dict[str, Tuple[int, int, int]]:
        """Return optimization ranges for walk-forward analysis."""
        return {
            "rsi_period": (7, 21, 1),
            "oversold": (20, 45, 5),
            "overbought": (55, 80, 5),
            "macd_fast": (8, 16, 1),
            "macd_slow": (20, 35, 1),
            "macd_signal": (7, 13, 1),
        }

    # ------------------------------------------------------------------
    #  Indicator calculations
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """
        Calculate Relative Strength Index.

        Formula:
            RSI = 100 - (100 / (1 + RS))
            RS  = Average Gain / Average Loss

        Uses Wilder's smoothing (exponential moving average of gains/losses).
        """
        delta = close.diff()

        # Separate gains and losses
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0))

        # Wilder's smoothing
        avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    @staticmethod
    def calculate_macd(
        close: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> pd.DataFrame:
        """
        Calculate MACD indicator.

        Returns DataFrame with:
            - macd_line:    Fast EMA - Slow EMA
            - signal_line:  EMA of macd_line
            - histogram:    macd_line - signal_line
        """
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return pd.DataFrame({
            "macd_line": macd_line,
            "signal_line": signal_line,
            "histogram": histogram,
        })

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
                - entry (bool): True on RSI oversold + MACD bullish cross
                - exit  (bool): True on RSI overbought + MACD bearish cross
                - rsi (float): RSI values
                - macd_line (float): MACD line values
                - signal_line (float): MACD signal line values
                - histogram (float): MACD histogram values
        """
        self.validate_dataframe(df)
        close = df["close"]

        # Calculate indicators
        rsi = self.calculate_rsi(close, self.rsi_period)
        macd_df = self.calculate_macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
        macd_line = macd_df["macd_line"]
        signal_line = macd_df["signal_line"]
        histogram = macd_df["histogram"]

        # Signal logic
        # BUY: RSI oversold (< threshold) AND MACD bullish crossover
        entries = (
            (rsi < self.oversold)
            & (macd_line > signal_line)
            & (macd_line.shift(1) <= signal_line.shift(1))
        )

        # SELL: RSI overbought (> threshold) AND MACD bearish crossover
        exits = (
            (rsi > self.overbought)
            & (macd_line < signal_line)
            & (macd_line.shift(1) >= signal_line.shift(1))
        )

        # Apply risk filters (cooldown, max position time)
        entries, exits = self.apply_risk_filters(entries, exits, df.index)

        return pd.DataFrame({
            "entry": entries,
            "exit": exits,
            "rsi": rsi,
            "macd_line": macd_line,
            "signal_line": signal_line,
            "histogram": histogram,
        }, index=df.index)

    # ------------------------------------------------------------------
    #  Live trading helpers
    # ------------------------------------------------------------------

    def calculate_indicators(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Calculate indicator values for the latest bar.
        Used by live trading to check conditions in real-time.

        Returns dict with current indicator state for logging/display.
        """
        close = df["close"]
        rsi = self.calculate_rsi(close, self.rsi_period)
        macd_df = self.calculate_macd(close, self.macd_fast, self.macd_slow, self.macd_signal)

        latest_rsi = rsi.iloc[-1]
        latest_macd = macd_df.iloc[-1]

        # Determine market regime
        if latest_rsi < self.oversold:
            regime = "OVERSOLD"
        elif latest_rsi > self.overbought:
            regime = "OVERBOUGHT"
        else:
            regime = "NEUTRAL"

        latest = {
            "rsi": latest_rsi,
            "rsi_regime": regime,
            "macd_line": latest_macd["macd_line"],
            "signal_line": latest_macd["signal_line"],
            "histogram": latest_macd["histogram"],
            "macd_trend": "BULLISH" if latest_macd["histogram"] > 0 else "BEARISH",
        }
        return latest

    def check_signal(self, df: pd.DataFrame) -> str:
        """
        Check if a signal is triggered on the latest bar.

        Returns:
            'BUY'  - RSI oversold + MACD bullish cross detected
            'SELL' - RSI overbought + MACD bearish cross detected
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

"""Signal strength scoring: converts raw strategy signal to 0.0-1.0 quality score.

Factors:
- Trend alignment (higher score when signal aligns with higher timeframe trend)
- Volume confirmation (higher score on above-average volume)
- Volatility regime (lower score in extreme volatility)
- Confluence (multiple strategies agreeing)
- Momentum confirmation (RSI/MACD direction)
- Pullback vs breakout context
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SignalStrengthScorer:
    """Score signal quality from 0.0 (worthless) to 1.0 (perfect).

    The final score is a weighted combination of sub-scores:

    * **Volume** (0.25 weight) – above-average volume = conviction.
    * **Volatility regime** (0.20 weight) – moderate vol is ideal.
    * **Trend alignment** (0.25 weight) – signal direction vs higher-TF trend.
    * **Confluence** (0.20 weight) – how many strategies agree.
    * **Momentum** (0.10 weight) – RSI / MACD confirming the signal.

    Usage::

        scorer = SignalStrengthScorer()
        score = scorer.score(
            candles,
            signal=1,
            signals_from_other_strategies=[1, 0, 1],
        )
        # score > 0.7 → strong signal, full position
        # score 0.3-0.7 → moderate signal, reduced position
        # score < 0.3 → weak signal, no trade
    """

    def __init__(
        self,
        volume_threshold: float = 1.5,
        vol_lookback: int = 20,
        trend_ema_fast: int = 9,
        trend_ema_slow: int = 21,
    ) -> None:
        self.volume_threshold = volume_threshold
        self.vol_lookback = vol_lookback
        self.trend_ema_fast = trend_ema_fast
        self.trend_ema_slow = trend_ema_slow

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def score(
        self,
        candles: pd.DataFrame,
        signal: int,
        signals_from_other_strategies: Optional[List[int]] = None,
    ) -> float:
        """Calculate signal strength in the range ``[0.0, 1.0]``.

        Args:
            candles: OHLCV DataFrame (needs ``close``, ``volume`` columns).
            signal: Raw signal (-1, 0, or 1).
            signals_from_other_strategies: List of signals from other
                strategies (each -1, 0, or 1).

        Returns:
            A float between 0.0 and 1.0.
        """
        if signal == 0:
            return 0.0

        scores: List[float] = []

        # 1. Volume confirmation (weight 0.25)
        scores.append(self._volume_score(candles) * 0.25)

        # 2. Volatility regime (weight 0.20)
        scores.append(self._volatility_score(candles) * 0.20)

        # 3. Trend alignment (weight 0.25)
        scores.append(self._trend_alignment_score(candles, signal) * 0.25)

        # 4. Strategy confluence (weight 0.20)
        if signals_from_other_strategies:
            scores.append(
                self._confluence_score(signal, signals_from_other_strategies)
                * 0.20
            )
        else:
            scores.append(0.10)  # neutral half-weight if no confluence data

        # 5. Momentum confirmation (weight 0.10)
        scores.append(self._momentum_score(candles, signal) * 0.10)

        total = sum(scores)
        clamped = max(0.0, min(total, 1.0))

        logger.debug(
            "Signal %d scored %.3f (components: %s)",
            signal,
            clamped,
            ", ".join(f"{s:.3f}" for s in scores),
        )
        return clamped

    def classify(self, score: float) -> str:
        """Classify a score into a textual bucket.

        Returns:
            ``"strong"``, ``"moderate"``, ``"weak"``, or ``"none"``.
        """
        if score >= 0.70:
            return "strong"
        elif score >= 0.40:
            return "moderate"
        elif score >= 0.15:
            return "weak"
        return "none"

    # ------------------------------------------------------------------ #
    #  Sub-scorers  (each returns 0.0-1.0)
    # ------------------------------------------------------------------ #

    def _volume_score(self, candles: pd.DataFrame) -> float:
        """Score based on current volume vs recent average.

        Ratio >= threshold  → 1.0
        Ratio < 1.0         → linear scale down to 0.0
        """
        if "volume" not in candles.columns:
            return 0.5
        avg_volume = candles["volume"].tail(self.vol_lookback).mean()
        current_volume = candles["volume"].iloc[-1]
        if avg_volume == 0 or np.isnan(avg_volume):
            return 0.5
        ratio = current_volume / avg_volume
        return min(ratio / self.volume_threshold, 1.0)

    def _volatility_score(self, candles: pd.DataFrame) -> float:
        """Score based on volatility regime.

        Moderate volatility is ideal for trend-following;
        extreme volatility is penalised heavily.

        Annualised vol thresholds (crypto-friendly):
        < 50%  → 1.0
        < 100% → 0.7
        < 150% → 0.4
        else   → 0.1
        """
        returns = candles["close"].pct_change().tail(self.vol_lookback)
        if len(returns.dropna()) < 5:
            return 0.5  # not enough data
        daily_vol = float(returns.std())
        annualised = daily_vol * np.sqrt(365)

        if annualised < 0.50:
            return 1.0
        elif annualised < 1.00:
            return 0.7
        elif annualised < 1.50:
            return 0.4
        return 0.1

    def _trend_alignment_score(
        self, candles: pd.DataFrame, signal: int
    ) -> float:
        """Score based on whether the signal aligns with the higher-timeframe trend.

        Uses EMA cross (fast vs slow) as a proxy for trend direction.
        Signal == trend  → 1.0
        Signal neutral   → 0.5
        Signal opposite  → 0.0
        """
        close = candles["close"]
        ema_fast = close.ewm(span=self.trend_ema_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.trend_ema_slow, adjust=False).mean()

        # Trend: +1 = bullish, -1 = bearish
        trend = 1 if ema_fast.iloc[-1] > ema_slow.iloc[-1] else -1

        if signal == trend:
            return 1.0
        elif signal == 0:
            return 0.5
        return 0.0

    def _confluence_score(
        self, signal: int, other_signals: List[int]
    ) -> float:
        """Score based on how many strategies agree with *signal*.

        100%% agreement  → 1.0
        0%% agreement   → 0.0
        """
        if not other_signals:
            return 0.5
        agreeing = sum(1 for s in other_signals if s == signal)
        return agreeing / len(other_signals)

    def _momentum_score(self, candles: pd.DataFrame, signal: int) -> float:
        """Score based on RSI and MACD confirming the signal.

        * RSI < 35 with buy signal  → strong confirmation
        * RSI > 65 with sell signal → strong confirmation
        * MACD histogram direction agrees → additional confirmation
        """
        close = candles["close"]

        # RSI (14-period)
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0)).abs()
        avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi_val = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0

        # MACD histogram
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        hist_val = float(histogram.iloc[-1]) if not np.isnan(histogram.iloc[-1]) else 0.0
        hist_prev = float(histogram.iloc[-2]) if len(histogram) > 1 and not np.isnan(histogram.iloc[-2]) else 0.0

        score = 0.5  # neutral base

        if signal == 1:  # buy
            if rsi_val < 35:
                score += 0.3  # oversold, good for long
            elif rsi_val > 70:
                score -= 0.3  # overbought, bad for long
            if hist_val > 0 and hist_val > hist_prev:
                score += 0.2  # MACD bullish and rising
            elif hist_val < 0:
                score -= 0.2  # MACD bearish
        elif signal == -1:  # sell
            if rsi_val > 65:
                score += 0.3  # overbought, good for short
            elif rsi_val < 30:
                score -= 0.3  # oversold, bad for short
            if hist_val < 0 and hist_val < hist_prev:
                score += 0.2  # MACD bearish and falling
            elif hist_val > 0:
                score -= 0.2  # MACD bullish

        return max(0.0, min(score, 1.0))


class MultiTimeframeScorer:
    """Score signals using higher-timeframe context.

    Combines a primary timeframe signal with a higher-timeframe trend
    assessment to penalise trades against the macro trend.

    Usage::

        mt_scorer = MultiTimeframeScorer(weight_htf=0.30)
        score = mt_scorer.score(
            candles_tf=candles_1h,
            candles_htf=candles_4h,
            signal=1,
            other_signals=[1, 0],
        )
    """

    def __init__(
        self,
        weight_htf: float = 0.30,
        volume_threshold: float = 1.5,
    ) -> None:
        self.weight_htf = weight_htf
        self._primary_scorer = SignalStrengthScorer(
            volume_threshold=volume_threshold
        )

    def score(
        self,
        candles_tf: pd.DataFrame,
        candles_htf: pd.DataFrame,
        signal: int,
        signals_from_other_strategies: Optional[List[int]] = None,
    ) -> float:
        """Calculate a multi-timeframe quality score.

        Args:
            candles_tf: Primary timeframe OHLCV.
            candles_htf: Higher-timeframe OHLCV for trend context.
            signal: Raw signal (-1, 0, 1).
            signals_from_other_strategies: Confluence data.

        Returns:
            Score in ``[0.0, 1.0]``.
        """
        # Primary score
        primary = self._primary_scorer.score(
            candles_tf, signal, signals_from_other_strategies
        )

        # Higher-timeframe trend alignment
        htf_align = self._primary_scorer._trend_alignment_score(
            candles_htf, signal
        )

        # Blend: primary gets (1 - weight_htf), HTF trend gets weight_htf
        blended = primary * (1.0 - self.weight_htf) + htf_align * self.weight_htf
        return max(0.0, min(blended, 1.0))

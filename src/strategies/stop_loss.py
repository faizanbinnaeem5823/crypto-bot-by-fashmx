"""Stop-loss calculator using ATR (Average True Range) and fixed methods.

Features:
- ATR-based dynamic stops (adjusts to volatility)
- Fixed percentage stops
- Support/resistance level stops
- Time-based stops (max holding period)
- Chandelier Exit (ATR-based trailing stop anchored to highest high / lowest low)

All prices use float (price data) for calculation, converted to Decimal for order submission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Literal, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class StopLevel:
    """Calculated stop-loss level.

    Attributes:
        price: The absolute stop-loss price.
        method: How the stop was derived ("atr", "fixed", "support", "chandelier").
        distance_pct: Distance from entry to stop, expressed as a positive percentage.
        atr_value: ATR reading at calculation time (if applicable).
    """

    price: float
    method: str
    distance_pct: float
    atr_value: Optional[float] = None


@dataclass
class TimeStopResult:
    """Result of a time-based stop check.

    Attributes:
        should_exit: True if max holding period has been exceeded.
        elapsed: Time elapsed since entry.
        reason: Human-readable explanation.
    """

    should_exit: bool
    elapsed: Optional[timedelta] = None
    reason: str = ""


class StopLossCalculator:
    """Calculate stop-loss levels for trades.

    Supports multiple methods:
    - **atr**: entry ± (ATR × multiplier). Adapts to current volatility.
    - **fixed**: entry ± fixed percentage. Simple and predictable.
    - **support**: Uses recent swing low (longs) or swing high (shorts).
    - **chandelier**: ATR-based trailing anchored to highest high / lowest low.

    Usage::

        calc = StopLossCalculator(
            method="atr", atr_period=14, atr_multiplier=2.0
        )
        stop = calc.calculate(
            candles, entry_price=50000.0, side="long"
        )
        print(f"Stop at {stop.price:,.2f} ({stop.distance_pct:.1f}% away)")
    """

    def __init__(
        self,
        method: Literal["atr", "fixed", "support", "chandelier"] = "atr",
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
        fixed_pct: float = 5.0,
        swing_lookback: int = 20,
    ) -> None:
        self.method = method
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.fixed_pct = fixed_pct
        self.swing_lookback = swing_lookback

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def calculate(
        self,
        candles: pd.DataFrame,
        entry_price: float,
        side: str,
    ) -> StopLevel:
        """Calculate stop-loss for a trade.

        Args:
            candles: OHLCV DataFrame (must contain ``high``, ``low``, ``close``).
            entry_price: Entry price of the trade.
            side: ``"long"`` or ``"short"``.

        Returns:
            A :class:`StopLevel` dataclass with the computed stop price.
        """
        if self.method == "atr":
            return self._atr_stop(candles, entry_price, side)
        elif self.method == "fixed":
            return self._fixed_stop(entry_price, side)
        elif self.method == "support":
            return self._support_stop(candles, entry_price, side)
        elif self.method == "chandelier":
            return self._chandelier_stop(candles, entry_price, side)
        else:
            raise ValueError(f"Unknown stop-loss method: {self.method}")

    def calculate_atr(
        self, candles: pd.DataFrame, period: Optional[int] = None
    ) -> pd.Series:
        """Compute the Average True Range (ATR) series.

        Args:
            candles: OHLCV DataFrame.
            period: Override period (defaults to ``self.atr_period``).

        Returns:
            A pandas Series of ATR values indexed like *candles*.
        """
        p = period or self.atr_period
        high_low = candles["high"] - candles["low"]
        high_close = (candles["high"] - candles["close"].shift()).abs()
        low_close = (candles["low"] - candles["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(p).mean()
        return atr

    # ------------------------------------------------------------------ #
    #  Internal methods
    # ------------------------------------------------------------------ #

    def _atr_stop(
        self, candles: pd.DataFrame, entry: float, side: str
    ) -> StopLevel:
        """ATR-based stop: ``entry ± (ATR × multiplier)``."""
        atr_series = self.calculate_atr(candles)
        atr = atr_series.iloc[-1]

        if side == "long":
            stop_price = entry - (atr * self.atr_multiplier)
        else:
            stop_price = entry + (atr * self.atr_multiplier)

        distance = abs(entry - stop_price) / entry * 100.0
        return StopLevel(
            price=stop_price,
            method="atr",
            distance_pct=distance,
            atr_value=atr,
        )

    def _fixed_stop(self, entry: float, side: str) -> StopLevel:
        """Fixed percentage stop."""
        if side == "long":
            stop_price = entry * (1.0 - self.fixed_pct / 100.0)
        else:
            stop_price = entry * (1.0 + self.fixed_pct / 100.0)
        return StopLevel(
            price=stop_price,
            method="fixed",
            distance_pct=self.fixed_pct,
        )

    def _support_stop(
        self, candles: pd.DataFrame, entry: float, side: str
    ) -> StopLevel:
        """Stop at recent support (longs) or resistance (shorts) level."""
        if side == "long":
            stop_price = float(candles["low"].tail(self.swing_lookback).min())
        else:
            stop_price = float(candles["high"].tail(self.swing_lookback).max())
        distance = abs(entry - stop_price) / entry * 100.0
        return StopLevel(price=stop_price, method="support", distance_pct=distance)

    def _chandelier_stop(
        self, candles: pd.DataFrame, entry: float, side: str
    ) -> StopLevel:
        """Chandelier Exit: ATR-based stop anchored to swing high/low.

        Longs:  highest_high - (ATR × multiplier)
        Shorts: lowest_low   + (ATR × multiplier)
        """
        atr_series = self.calculate_atr(candles)
        atr = float(atr_series.iloc[-1])

        if side == "long":
            highest = float(candles["high"].tail(self.swing_lookback).max())
            stop_price = highest - (atr * self.atr_multiplier)
            # Ensure stop is below entry
            stop_price = min(stop_price, entry)
        else:
            lowest = float(candles["low"].tail(self.swing_lookback).min())
            stop_price = lowest + (atr * self.atr_multiplier)
            # Ensure stop is above entry
            stop_price = max(stop_price, entry)

        distance = abs(entry - stop_price) / entry * 100.0
        return StopLevel(
            price=stop_price,
            method="chandelier",
            distance_pct=distance,
            atr_value=atr,
        )

    # ------------------------------------------------------------------ #
    #  Time-based stop
    # ------------------------------------------------------------------ #

    def check_time_stop(
        self,
        entry_time: datetime,
        current_time: datetime,
        max_hold_hours: float = 48.0,
    ) -> TimeStopResult:
        """Check if a position has exceeded its maximum holding period.

        Args:
            entry_time: When the trade was opened.
            current_time: Current timestamp.
            max_hold_hours: Maximum allowed hold time in hours.

        Returns:
            :class:`TimeStopResult` indicating whether to exit.
        """
        elapsed = current_time - entry_time
        max_delta = timedelta(hours=max_hold_hours)
        should_exit = elapsed > max_delta
        reason = (
            f"Time stop triggered: held for {elapsed.total_seconds() / 3600:.1f}h "
            f"(max {max_hold_hours}h)"
            if should_exit
            else ""
        )
        return TimeStopResult(
            should_exit=should_exit, elapsed=elapsed, reason=reason
        )


class DynamicStopAdjuster:
    """Dynamically tighten stop-loss as trade moves in favour.

    Each time price moves ``step_pct`` in profit, the stop is moved
    ``tighten_pct`` closer to the entry (breakeven) and then beyond.

    Usage::

        adjuster = DynamicStopAdjuster(step_pct=2.0, tighten_pct=1.0)
        adjuster.set_entry(entry=50000.0, stop=47500.0, side="long")

        new_stop = adjuster.adjust(current_price=51000.0)
        # new_stop > original_stop  → stop has been tightened
    """

    def __init__(self, step_pct: float = 2.0, tighten_pct: float = 1.0) -> None:
        self.step_pct = step_pct
        self.tighten_pct = tighten_pct
        self._entry: Optional[float] = None
        self._initial_stop: Optional[float] = None
        self._current_stop: Optional[float] = None
        self._side: str = ""
        self._steps_triggered: int = 0

    def set_entry(self, entry: float, stop: float, side: str) -> None:
        """Record trade parameters."""
        self._entry = entry
        self._initial_stop = stop
        self._current_stop = stop
        self._side = side
        self._steps_triggered = 0

    def adjust(self, current_price: float) -> float:
        """Re-evaluate stop given the latest price.

        Returns:
            The (potentially tightened) stop price.
        """
        if self._entry is None or self._initial_stop is None:
            raise RuntimeError("Call set_entry() before adjust()")

        entry = self._entry
        side = self._side
        risk = abs(entry - self._initial_stop)

        # Determine floating PnL as multiple of initial risk
        if side == "long":
            pnl_r = (current_price - entry) / risk if risk else 0.0
        else:
            pnl_r = (entry - current_price) / risk if risk else 0.0

        if pnl_r <= 0:
            return self._current_stop  # type: ignore[return-value]

        # How many step increments have we crossed?
        steps = int(pnl_r * 100.0 / self.step_pct)
        if steps <= self._steps_triggered:
            return self._current_stop  # type: ignore[return-value]

        # Tighten stop: move it by tighten_pct per new step
        steps_to_apply = steps - self._steps_triggered
        move = self.tighten_pct / 100.0 * entry * steps_to_apply

        if side == "long":
            self._current_stop = self._current_stop + move  # type: ignore[operator]
            # Cap at breakeven initially, then let it trail
            self._current_stop = min(self._current_stop, entry)
        else:
            self._current_stop = self._current_stop - move  # type: ignore[operator]
            self._current_stop = max(self._current_stop, entry)

        self._steps_triggered = steps
        logger.debug(
            "Stop tightened to %.2f (+%d steps)", self._current_stop, steps_to_apply
        )
        return self._current_stop  # type: ignore[return-value]

    def reset(self) -> None:
        """Clear internal state for reuse."""
        self._entry = None
        self._initial_stop = None
        self._current_stop = None
        self._side = ""
        self._steps_triggered = 0

"""Take-profit and trailing stop logic.

Features:
- Fixed R:R (risk:reward) targets (1:1.5, 1:2, 1:3)
- Trailing stop that follows price
- Partial profit-taking (e.g. 50%% at 1:1, 50%% at 1:2)
- ATR-based dynamic take-profit
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Literal, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ProfitLevel:
    """A single take-profit level.

    Attributes:
        price: Absolute price target.
        ratio: Risk:reward ratio for this level (e.g. 2.0 means 1:2).
        percentage: Percentage of the position to close here (0-100).
    """

    price: float
    ratio: float
    percentage: float


@dataclass
class TakeProfitPlan:
    """Complete take-profit plan for a trade.

    Attributes:
        levels: Ordered list of profit-taking levels.
        method: ``"fixed_rr"``, ``"trailing"``, or ``"atr"``.
        trailing_activation: R:R level at which a trailing stop activates
            (only for trailing method).
    """

    levels: List[ProfitLevel]
    method: str
    trailing_activation: Optional[float] = None


class TakeProfitCalculator:
    """Calculate take-profit levels.

    Supports three methods:

    - **fixed_rr**: Targets based on fixed risk:reward ratios.
    - **atr**: ATR-based dynamic targets that adapt to volatility.
    - **trailing**: Single target + trailing stop activation.

    Usage::

        calc = TakeProfitCalculator(method="fixed_rr", risk_reward="1:2")
        plan = calc.calculate(entry=50000.0, stop=47500.0, side="long")
        for lvl in plan.levels:
            print(
                f"Close {lvl.percentage}% at {lvl.price:,.2f} (R:R {lvl.ratio})"
            )
    """

    def __init__(
        self,
        method: Literal["fixed_rr", "trailing", "atr"] = "fixed_rr",
        risk_reward: str = "1:2",
        trailing_pct: float = 5.0,
        partial_levels: Optional[List[dict]] = None,
        atr_period: int = 14,
        atr_multiplier: float = 3.0,
    ) -> None:
        self.method = method
        self.risk_reward = risk_reward
        self.trailing_pct = trailing_pct
        self.partial_levels = partial_levels or [
            {"ratio": 1.0, "percentage": 50.0},
            {"ratio": 2.0, "percentage": 50.0},
        ]
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def calculate(
        self,
        entry: float,
        stop: float,
        side: str,
        candles: Optional[pd.DataFrame] = None,
    ) -> TakeProfitPlan:
        """Calculate take-profit plan.

        Args:
            entry: Entry price.
            stop: Stop-loss price.
            side: ``"long"`` or ``"short"``.
            candles: OHLCV DataFrame (required for ``method="atr"``).

        Returns:
            A :class:`TakeProfitPlan` with one or more profit levels.
        """
        if self.method == "fixed_rr":
            return self._fixed_rr(entry, stop, side)
        elif self.method == "atr":
            if candles is None:
                raise ValueError("candles DataFrame required for ATR-based TP")
            return self._atr_tp(entry, stop, side, candles)
        elif self.method == "trailing":
            return self._trailing_plan(entry, stop, side)
        else:
            raise ValueError(f"Unknown TP method: {self.method}")

    # ------------------------------------------------------------------ #
    #  Internal methods
    # ------------------------------------------------------------------ #

    def _fixed_rr(self, entry: float, stop: float, side: str) -> TakeProfitPlan:
        """Fixed risk:reward targets with optional partial exits."""
        risk = abs(entry - stop)
        levels: List[ProfitLevel] = []

        for lvl in self.partial_levels:
            ratio = float(lvl["ratio"])
            pct = float(lvl["percentage"])
            if side == "long":
                tp_price = entry + (risk * ratio)
            else:
                tp_price = entry - (risk * ratio)
            levels.append(ProfitLevel(price=tp_price, ratio=ratio, percentage=pct))

        return TakeProfitPlan(levels=levels, method="fixed_rr")

    def _atr_tp(
        self, entry: float, stop: float, side: str, candles: pd.DataFrame
    ) -> TakeProfitPlan:
        """ATR-based dynamic take-profit.

        TP = entry ± (ATR × multiplier).  The multiplier is larger than the
        stop multiplier so the reward exceeds the risk.
        """
        high_low = candles["high"] - candles["low"]
        high_close = (candles["high"] - candles["close"].shift()).abs()
        low_close = (candles["low"] - candles["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = float(tr.rolling(self.atr_period).mean().iloc[-1])

        if side == "long":
            tp_price = entry + (atr * self.atr_multiplier)
        else:
            tp_price = entry - (atr * self.atr_multiplier)

        # Compute actual R:R
        risk = abs(entry - stop)
        reward = abs(tp_price - entry)
        ratio = reward / risk if risk else 0.0

        levels = [ProfitLevel(price=tp_price, ratio=ratio, percentage=100.0)]
        return TakeProfitPlan(levels=levels, method="atr")

    def _trailing_plan(self, entry: float, stop: float, side: str) -> TakeProfitPlan:
        """Single target + trailing stop activation point."""
        rr_mult = float(self.risk_reward.split(":")[1])
        risk = abs(entry - stop)

        if side == "long":
            tp_price = entry + (risk * rr_mult)
        else:
            tp_price = entry - (risk * rr_mult)

        levels = [ProfitLevel(price=tp_price, ratio=rr_mult, percentage=100.0)]
        activation = rr_mult * 0.5  # trailing activates at 50% of target R:R
        return TakeProfitPlan(
            levels=levels,
            method="trailing",
            trailing_activation=activation,
        )


# ========================================================================
#  Trailing Stop
# ========================================================================


class TrailingStop:
    """Trailing stop that follows favourable price movement.

    The stop is initially placed at the original stop-loss price.
    Once price moves ``activation_rr`` times the initial risk in profit,
    the trailing stop activates and tracks the best price seen, keeping a
    ``trail_pct`` buffer behind it.

    Usage::

        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000.0, stop=47500.0, side="long")

        new_stop = ts.update(current_price=52000.0)
        if new_stop is not None:
            print(f"Trailing stop moved to {new_stop:,.2f}")

        if ts.should_exit(current_price=48000.0):
            print("Hit trailing stop – exit now!")
    """

    def __init__(
        self,
        activation_rr: float = 1.0,
        trail_pct: float = 5.0,
    ) -> None:
        self.activation_rr = activation_rr
        self.trail_pct = trail_pct
        self.active: bool = False
        self._best_price: float = 0.0
        self._stop_price: float = 0.0
        self._entry: float = 0.0
        self._initial_stop: float = 0.0
        self._side: str = ""
        self._risk: float = 0.0

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def activate(self, entry: float, stop: float, side: str) -> None:
        """Activate trailing stop for a position.

        Args:
            entry: Entry price.
            stop: Original stop-loss price.
            side: ``"long"`` or ``"short"``.
        """
        self._entry = entry
        self._initial_stop = stop
        self._stop_price = stop
        self._side = side
        self._risk = abs(entry - stop)
        self._best_price = entry
        self.active = False

        logger.debug(
            "TrailingStop activated: entry=%.2f stop=%.2f side=%s risk=%.2f",
            entry,
            stop,
            side,
            self._risk,
        )

    def update(self, current_price: float) -> Optional[float]:
        """Update trailing stop with the latest price.

        Returns:
            The new stop price if it moved, otherwise ``None``.
        """
        side = self._side
        risk = self._risk

        if risk == 0:
            return None

        # Track best price
        if side == "long":
            if current_price > self._best_price:
                self._best_price = current_price
        else:
            if current_price < self._best_price:
                self._best_price = current_price

        # Check activation
        if not self.active:
            pnl_r = (
                (current_price - self._entry) / risk
                if side == "long"
                else (self._entry - current_price) / risk
            )
            if pnl_r >= self.activation_rr:
                self.active = True
                logger.info(
                    "Trailing stop activated at R:R %.2f (price=%.2f)",
                    pnl_r,
                    current_price,
                )
            else:
                return None

        # Calculate new trailing stop
        if side == "long":
            new_stop = self._best_price * (1.0 - self.trail_pct / 100.0)
            if new_stop > self._stop_price:
                old_stop = self._stop_price
                self._stop_price = new_stop
                logger.debug(
                    "Trailing stop long: %.2f → %.2f", old_stop, new_stop
                )
                return new_stop
        else:
            new_stop = self._best_price * (1.0 + self.trail_pct / 100.0)
            if new_stop < self._stop_price:
                old_stop = self._stop_price
                self._stop_price = new_stop
                logger.debug(
                    "Trailing stop short: %.2f → %.2f", old_stop, new_stop
                )
                return new_stop

        return None

    def should_exit(self, current_price: float) -> bool:
        """Return ``True`` if the current price has hit the trailing stop."""
        if not self.active:
            return False
        if self._side == "long":
            return current_price <= self._stop_price
        return current_price >= self._stop_price

    @property
    def current_stop(self) -> float:
        """Return the current stop price."""
        return self._stop_price

    def reset(self) -> None:
        """Reset to inactive state for reuse."""
        self.active = False
        self._best_price = 0.0
        self._stop_price = 0.0
        self._entry = 0.0
        self._initial_stop = 0.0
        self._side = ""
        self._risk = 0.0

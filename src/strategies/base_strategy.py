"""
Abstract base strategy class for CryptoBot.

All strategies must inherit from this class and implement:
    - generate_signals(df) -> DataFrame with 'entry' and 'exit' boolean columns
    - get_parameters() -> Dict of strategy parameters (for logging/serialization)
    - set_parameters(**kwargs) -> Update parameters (for optimization)

This design lets the same strategy class run in both backtests (via vectorbt)
and live trading (via the Binance websocket feed).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd


@dataclass
class Signal:
    """A single trading signal."""
    timestamp: pd.Timestamp
    side: str  # 'BUY' or 'SELL'
    price: float
    confidence: float = 1.0  # 0.0 to 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Signal side must be BUY or SELL, got {self.side}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Confidence must be between 0 and 1, got {self.confidence}")


@dataclass
class StrategyParameters:
    """Container for strategy parameter ranges (used in optimization)."""
    name: str
    description: str
    defaults: Dict[str, Any]
    ranges: Dict[str, tuple]  # param_name -> (min, max, step)


class BaseStrategy(ABC):
    """
    Abstract base class for all CryptoBot trading strategies.

    Subclasses must implement:
        - generate_signals(df) -> pd.DataFrame
        - get_name() -> str

    The base class provides utility methods for:
        - Parameter management
        - Risk checks (cooldown, max position time)
        - Signal filtering
        - State serialization for live trading
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        """
        Initialize strategy with parameters.

        Args:
            params: Dict of parameter overrides. Uses defaults if not provided.
        """
        self._params = self.default_parameters()
        if params:
            self._params.update(params)
        self._state = {
            "last_signal_time": None,
            "position_open": False,
            "entry_price": None,
            "entry_time": None,
            "trade_count": 0,
            "last_trade_result": None,
        }
        self._cooldown_periods = self._params.get("cooldown_periods", 0)
        self._max_position_bars = self._params.get("max_position_bars", None)

    # ------------------------------------------------------------------
    #  Abstract methods subclasses must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate entry/exit signals from OHLCV data.

        Args:
            df: DataFrame with columns ['open', 'high', 'low', 'close', 'volume']
                and a DatetimeIndex.

        Returns:
            DataFrame with at minimum:
                - 'entry' (bool): True where a long entry is triggered
                - 'exit'  (bool): True where a long exit is triggered
            May also include 'confidence' (float 0-1) and other columns.
        """
        ...

    @abstractmethod
    def get_name(self) -> str:
        """Return the strategy name for reporting."""
        ...

    # ------------------------------------------------------------------
    #  Parameter management
    # ------------------------------------------------------------------

    @classmethod
    def default_parameters(cls) -> Dict[str, Any]:
        """
        Return the default parameter dict.
        Override in subclass with strategy-specific defaults.
        """
        return {
            "cooldown_periods": 0,      # bars to wait between trades
            "max_position_bars": None,  # auto-exit after N bars (None = disabled)
        }

    def get_parameters(self) -> Dict[str, Any]:
        """Return current parameter values."""
        return dict(self._params)

    def set_parameters(self, **kwargs) -> "BaseStrategy":
        """
        Update strategy parameters (returns self for chaining).

        Usage:
            strategy.set_parameters(fast=5, slow=15)
        """
        for key, value in kwargs.items():
            if key in self._params:
                self._params[key] = value
            else:
                raise KeyError(f"Unknown parameter '{key}' for {self.get_name()}")
        # Refresh derived settings
        self._cooldown_periods = self._params.get("cooldown_periods", 0)
        self._max_position_bars = self._params.get("max_position_bars", None)
        return self

    @classmethod
    def parameter_ranges(cls) -> Dict[str, tuple]:
        """
        Return parameter optimization ranges.
        Override in subclass to enable walk-forward optimization.

        Returns:
            Dict[param_name] -> (min_value, max_value, step)
        """
        return {}

    # ------------------------------------------------------------------
    #  Signal generation helpers (used by both backtest + live)
    # ------------------------------------------------------------------

    def apply_risk_filters(
        self,
        entries: pd.Series,
        exits: pd.Series,
        index: pd.DatetimeIndex,
    ) -> tuple[pd.Series, pd.Series]:
        """
        Apply cooldown and max-position-time filters to raw signals.

        This is called automatically by generate_signals() in concrete
        implementations, or can be used standalone for live trading.
        """
        entries = entries.copy()
        exits = exits.copy()

        # --- Cooldown filter ---
        if self._cooldown_periods > 0:
            last_entry_idx = -self._cooldown_periods - 1
            for i in range(len(entries)):
                if entries.iloc[i]:
                    if i - last_entry_idx <= self._cooldown_periods:
                        entries.iloc[i] = False
                    else:
                        last_entry_idx = i

        # --- Max position time filter ---
        if self._max_position_bars is not None:
            pos_start = None
            for i in range(len(index)):
                if entries.iloc[i] and pos_start is None:
                    pos_start = i
                elif exits.iloc[i] and pos_start is not None:
                    pos_start = None
                elif pos_start is not None and (i - pos_start) >= self._max_position_bars:
                    exits.iloc[i] = True
                    pos_start = None

        return entries, exits

    # ------------------------------------------------------------------
    #  Live trading state helpers
    # ------------------------------------------------------------------

    def reset_state(self) -> None:
        """Reset internal state (call on new trading session)."""
        self._state.update({
            "last_signal_time": None,
            "position_open": False,
            "entry_price": None,
            "entry_time": None,
            "trade_count": 0,
            "last_trade_result": None,
        })

    def on_entry(self, timestamp: pd.Timestamp, price: float) -> None:
        """Call when strategy enters a position (live trading)."""
        self._state["position_open"] = True
        self._state["entry_price"] = price
        self._state["entry_time"] = timestamp
        self._state["trade_count"] += 1

    def on_exit(self, timestamp: pd.Timestamp, price: float) -> None:
        """Call when strategy exits a position (live trading)."""
        if self._state["entry_price"]:
            pnl_pct = (price - self._state["entry_price"]) / self._state["entry_price"]
            self._state["last_trade_result"] = pnl_pct
        self._state["position_open"] = False
        self._state["entry_price"] = None
        self._state["entry_time"] = None
        self._state["last_signal_time"] = timestamp

    def get_state(self) -> Dict[str, Any]:
        """Serialize current state (for persistence / recovery)."""
        return dict(self._state)

    def set_state(self, state: Dict[str, Any]) -> None:
        """Restore state from serialized dict."""
        self._state.update(state)

    # ------------------------------------------------------------------
    #  Utility
    # ------------------------------------------------------------------

    @staticmethod
    def validate_dataframe(df: pd.DataFrame) -> None:
        """Verify the input DataFrame has required columns."""
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {missing}")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("DataFrame index must be a DatetimeIndex")
        if len(df) < 10:
            raise ValueError("DataFrame must have at least 10 rows")

    def __repr__(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in self._params.items())
        return f"{self.__class__.__name__}({params})"

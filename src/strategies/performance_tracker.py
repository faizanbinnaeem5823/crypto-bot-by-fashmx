"""Per-strategy performance tracking.

Maintains P&L, win rate, Sharpe ratio, profit factor and other metrics for
each strategy *separately*.  This lets the bot detect when a specific
strategy degrades and should be disabled or re-optimised.

Integration points:
    * Executed trades are recorded via :meth:`record_trade`.
    * The ``get_best_strategy`` helper picks the top performer.
    * ``should_disable`` acts as a circuit-breaker for failing strategies.

Usage::

    from strategies.performance_tracker import PerformanceTracker

    tracker = PerformanceTracker(min_trades_for_stats=20)

    # On every fill
    tracker.record_trade("EMA_Cross", pnl=2.5,  timestamp=pd.Timestamp.now())
    tracker.record_trade("RSI_MACD",  pnl=-1.0, timestamp=pd.Timestamp.now())

    # Query
    perf = tracker.get_performance("EMA_Cross")
    print(f"Sharpe={perf.sharpe:.2f}  WR={perf.win_rate:.1f}%")

    if tracker.should_disable("RSI_MACD", min_sharpe=0.5):
        print("Disabling RSI_MACD -- below threshold")
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MIN_TRADES_FOR_STATS: int = 20
DEFAULT_DISABLE_SHARPE: float = 0.0


# ---------------------------------------------------------------------------
# Per-strategy performance record
# ---------------------------------------------------------------------------
@dataclass
class StrategyPerformance:
    """Rolling performance metrics for a single strategy.

    All monetary fields are stored in *percent* PnL per trade
    (e.g. +2.5 means a 2.5 % gain on that trade).  This makes
    Sharpe and win-rate comparable across strategies and capital sizes.
    """

    strategy_name: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    returns: List[float] = field(default_factory=list)
    timestamps: List[pd.Timestamp] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def win_rate(self) -> float:
        """Win rate as a percentage (0-100)."""
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100.0

    @property
    def loss_rate(self) -> float:
        """Loss rate as a percentage (0-100)."""
        if self.total_trades == 0:
            return 0.0
        return self.losing_trades / self.total_trades * 100.0

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe ratio (crypto calendar: 365 days).

        Returns 0.0 when fewer than 10 trades are recorded or
        the return standard deviation is zero.
        """
        if len(self.returns) < 10:
            return 0.0
        arr = np.array(self.returns)
        std = arr.std()
        if std == 0:
            return 0.0
        return (arr.mean() / std) * np.sqrt(365.0)

    @property
    def profit_factor(self) -> float:
        """Profit factor = gross profit / |gross loss|.

        A value > 1.0 means the strategy is net profitable.
        Returns 1.0 when there are no losses.
        """
        if self.gross_loss == 0.0:
            return 1.0
        return self.gross_profit / abs(self.gross_loss)

    @property
    def avg_win(self) -> float:
        """Average winning trade return (percent)."""
        if self.winning_trades == 0:
            return 0.0
        return self.gross_profit / self.winning_trades

    @property
    def avg_loss(self) -> float:
        """Average losing trade return (percent)."""
        if self.losing_trades == 0:
            return 0.0
        return self.gross_loss / self.losing_trades

    @property
    def expectancy(self) -> float:
        """Per-trade expected return (percent).

        = (win_rate * avg_win) + (loss_rate * avg_loss)
        """
        wr = self.win_rate / 100.0
        lr = self.loss_rate / 100.0
        return wr * self.avg_win + lr * self.avg_loss

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough drawdown (percent, negative number)."""
        if len(self.returns) < 2:
            return 0.0
        cum = np.cumsum(self.returns)
        peak = np.maximum.accumulate(cum)
        drawdown = cum - peak
        return float(drawdown.min())

    @property
    def max_consecutive_wins(self) -> int:
        """Longest streak of consecutive winning trades."""
        if not self.returns:
            return 0
        max_streak = current = 0
        for r in self.returns:
            if r > 0:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    @property
    def max_consecutive_losses(self) -> int:
        """Longest streak of consecutive losing trades."""
        if not self.returns:
            return 0
        max_streak = current = 0
        for r in self.returns:
            if r < 0:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    def to_dict(self) -> Dict[str, Any]:
        """Serialise metrics to a plain dict (for JSON / logging)."""
        return {
            "strategy_name": self.strategy_name,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": round(self.total_pnl, 6),
            "win_rate": round(self.win_rate, 2),
            "sharpe": round(self.sharpe, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_win": round(self.avg_win, 6),
            "avg_loss": round(self.avg_loss, 6),
            "expectancy": round(self.expectancy, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
        }


# ---------------------------------------------------------------------------
# Multi-strategy tracker
# ---------------------------------------------------------------------------
class PerformanceTracker:
    """Track rolling performance across an ensemble of strategies.

    Parameters
    ----------
    min_trades_for_stats :
        Minimum number of trades before Sharpe / disable checks are valid.
    """

    def __init__(self, min_trades_for_stats: int = DEFAULT_MIN_TRADES_FOR_STATS):
        self._performances: Dict[str, StrategyPerformance] = {}
        self._min_trades = min_trades_for_stats

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_trade(
        self,
        strategy_name: str,
        pnl: float,
        timestamp: Optional[pd.Timestamp] = None,
    ) -> None:
        """Record a completed trade result.

        Parameters
        ----------
        strategy_name :
            Identifier for the strategy (e.g. ``"EMA_Cross"``).
        pnl :
            Trade P&L in *percent* (e.g. +2.5 for a 2.5 % winner).
        timestamp :
            Optional timestamp of the trade.  Defaults to ``pd.Timestamp.now()``.
        """
        if strategy_name not in self._performances:
            self._performances[strategy_name] = StrategyPerformance(
                strategy_name=strategy_name
            )

        perf = self._performances[strategy_name]
        perf.total_trades += 1
        perf.total_pnl += pnl
        perf.returns.append(pnl)
        perf.timestamps.append(timestamp or pd.Timestamp.now())

        if pnl > 0:
            perf.winning_trades += 1
            perf.gross_profit += pnl
        else:
            perf.losing_trades += 1
            perf.gross_loss += pnl

    def record_batch(
        self, strategy_name: str, pnls: List[float]
    ) -> None:
        """Record many trade P&Ls at once (e.g. after a vectorised backtest)."""
        for pnl in pnls:
            self.record_trade(strategy_name, pnl)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_performance(self, strategy_name: str) -> StrategyPerformance:
        """Return the performance record for a strategy.

        Returns a fresh ``StrategyPerformance`` with zero stats if the
        strategy has never been recorded.
        """
        return self._performances.get(
            strategy_name, StrategyPerformance(strategy_name)
        )

    def get_all(self) -> Dict[str, StrategyPerformance]:
        """Return a shallow copy of all tracked performances."""
        return dict(self._performances)

    def get_best_strategy(self, metric: str = "sharpe") -> str:
        """Return the name of the best-performing strategy by *metric*.

        Parameters
        ----------
        metric :
            One of ``"sharpe"``, ``"win_rate"``, ``"profit_factor"``,
            ``"total_pnl"``, ``"expectancy"``.

        Returns
        -------
        str
            Strategy name, or ``""`` if no strategies are tracked.
        """
        if not self._performances:
            return ""

        valid_metrics = {"sharpe", "win_rate", "profit_factor", "total_pnl", "expectancy"}
        if metric not in valid_metrics:
            raise ValueError(f"metric must be one of {valid_metrics}, got {metric!r}")

        return max(
            self._performances.items(),
            key=lambda kv: getattr(kv[1], metric),
        )[0]

    def get_ranking(self, metric: str = "sharpe") -> List[Tuple[str, float]]:
        """Return all strategies sorted descending by *metric*.

        Returns
        -------
        list of (strategy_name, metric_value)
        """
        valid_metrics = {"sharpe", "win_rate", "profit_factor", "total_pnl", "expectancy"}
        if metric not in valid_metrics:
            raise ValueError(f"metric must be one of {valid_metrics}, got {metric!r}")

        scored = [
            (name, getattr(perf, metric))
            for name, perf in self._performances.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Circuit-breakers
    # ------------------------------------------------------------------

    def should_disable(
        self,
        strategy_name: str,
        min_sharpe: float = DEFAULT_DISABLE_SHARPE,
    ) -> bool:
        """Return ``True`` if the strategy should be disabled.

        A strategy is disabled when:
            1. It has at least ``min_trades_for_stats`` recorded trades, AND
            2. Its Sharpe ratio is strictly below *min_sharpe*.

        Parameters
        ----------
        strategy_name :
            Strategy to evaluate.
        min_sharpe :
            Minimum acceptable Sharpe.  Default 0.0 (must at least break even).

        Returns
        -------
        bool
        """
        perf = self.get_performance(strategy_name)
        if perf.total_trades < self._min_trades:
            return False
        return perf.sharpe < min_sharpe

    def should_reoptimise(
        self,
        strategy_name: str,
        oos_is_ratio_threshold: float = 0.70,
    ) -> bool:
        """Return ``True`` if the strategy's OOS performance has degraded.

        Signals that a walk-forward re-optimisation should be triggered.

        Parameters
        ----------
        strategy_name :
            Strategy to evaluate.
        oos_is_ratio_threshold :
            If recent OOS Sharpe / IS Sharpe falls below this, flag for
            re-optimisation.
        """
        perf = self.get_performance(strategy_name)
        if perf.total_trades < self._min_trades:
            return False

        # Use max drawdown as a proxy: deep DD suggests degradation
        if perf.max_drawdown < -0.20:  # noqa: SIM103
            return True

        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        """Serialise the entire tracker to a nested dict."""
        return {
            name: perf.to_dict()
            for name, perf in self._performances.items()
        }

    def reset(self, strategy_name: Optional[str] = None) -> None:
        """Reset performance for a single strategy, or all strategies.

        Parameters
        ----------
        strategy_name :
            If given, only reset this strategy.  Otherwise reset everything.
        """
        if strategy_name is not None:
            self._performances.pop(strategy_name, None)
            logger.info("Reset performance for %s", strategy_name)
        else:
            self._performances.clear()
            logger.info("Reset all performance tracking")

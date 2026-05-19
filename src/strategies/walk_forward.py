"""Walk-Forward Analysis (WFA) for trading strategy validation.

The gold standard for proving a strategy is not overfit.

Method:
    1. Split data into multiple train / test windows.
    2. Optimise parameters on the training data (IS).
    3. Evaluate on the test data (OOS) -- unseen during optimisation.
    4. Walk the window forward.
    5. Aggregate OOS results.

Gates (all must pass before paper trading):
    - Deflated Sharpe  > 0.95
    - PBO              < 0.50
    - OOS Sharpe       >= 0.70 * IS Sharpe   (within 30 % of IS)

Usage:
    from strategies.walk_forward import WalkForwardAnalyzer

    wfa = WalkForwardAnalyzer(
        strategy_class=EMACrossoverStrategy,
        param_grid={"fast": [5, 9, 12], "slow": [21, 26, 50]},
        train_size=252,   # ~1 year of daily candles
        test_size=63,     # ~3 months of daily candles
        n_splits=5,
    )

    results = wfa.run(candles_df)
    print(f"OOS Sharpe : {results.oos_sharpe:.2f}")
    print(f"PBO        : {results.pbo:.2f}")
    print(f"Deflated SR: {results.deflated_sharpe:.2f}")
    print(f"Pass gates?  {results.passes_gate}")
"""

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gate thresholds
# ---------------------------------------------------------------------------
DEFLATED_SHARPE_THRESHOLD: float = 0.95
PBO_THRESHOLD: float = 0.50
OOS_IS_RATIO_THRESHOLD: float = 0.70


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class WFAWindow:
    """Single walk-forward window results."""

    split_idx: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    best_params: Dict[str, Any]
    is_sharpe: float
    oos_sharpe: float
    is_cagr: float = 0.0
    oos_cagr: float = 0.0
    is_maxdd: float = 0.0
    oos_maxdd: float = 0.0


@dataclass
class WFAResults:
    """Aggregated walk-forward analysis results."""

    windows: List[WFAWindow] = field(default_factory=list)
    oos_sharpe: float = 0.0
    oos_sharpes: List[float] = field(default_factory=list)
    is_sharpe: float = 0.0
    is_sharpes: List[float] = field(default_factory=list)
    oos_cagr: float = 0.0
    oos_maxdd: float = 0.0
    pbo: float = 1.0
    deflated_sharpe: float = 0.0
    oos_is_ratio: float = 0.0
    passes_gate: bool = False

    def summary(self) -> str:
        """Return a human-readable summary of results."""
        lines = [
            "=" * 55,
            "  Walk-Forward Analysis Results",
            "=" * 55,
            f"  Windows evaluated : {len(self.windows)}",
            f"  Mean IS Sharpe    : {self.is_sharpe:>8.3f}",
            f"  Mean OOS Sharpe   : {self.oos_sharpe:>8.3f}",
            f"  OOS / IS ratio    : {self.oos_is_ratio:>8.3f}  (min: {OOS_IS_RATIO_THRESHOLD})",
            f"  PBO               : {self.pbo:>8.3f}  (max: {PBO_THRESHOLD})",
            f"  Deflated Sharpe   : {self.deflated_sharpe:>8.3f}  (min: {DEFLATED_SHARPE_THRESHOLD})",
            f"  OOS CAGR          : {self.oos_cagr:>8.3f}",
            f"  OOS Max Drawdown  : {self.oos_maxdd:>8.3f}",
            "-" * 55,
            f"  GATE RESULT       : {'PASS' if self.passes_gate else 'FAIL'}",
            "=" * 55,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------
class WalkForwardAnalyzer:
    """Walk-forward analysis engine.

    Parameters
    ----------
    strategy_class :
        Concrete ``BaseStrategy`` subclass to optimise.
    param_grid :
        Mapping of parameter name -> list of candidate values,
        e.g. ``{"fast": [5, 9, 12], "slow": [21, 26, 50]}``.
    train_size :
        Number of candles in each training (IS) window.
    test_size :
        Number of candles in each test (OOS) window.
    n_splits :
        How many walk-forward windows to generate.
    annualisation_factor :
        Square-root factor for annualising Sharpe.
        365 for crypto (trades every day), 252 for equities.
    fee :
        Per-trade fee as a fraction (e.g. 0.001 for 0.1 %).
    slippage :
        Slippage per trade as a fraction.
    """

    def __init__(
        self,
        strategy_class: Type[BaseStrategy],
        param_grid: Dict[str, List[Any]],
        train_size: int = 252,
        test_size: int = 63,
        n_splits: int = 5,
        annualisation_factor: float = 365.0,
        fee: float = 0.001,
        slippage: float = 0.0005,
    ):
        self.strategy_class = strategy_class
        self.param_grid = param_grid
        self.train_size = train_size
        self.test_size = test_size
        self.n_splits = n_splits
        self.annualisation_factor = annualisation_factor
        self.fee = fee
        self.slippage = slippage

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, candles: pd.DataFrame) -> WFAResults:
        """Run the complete walk-forward analysis pipeline.

        Parameters
        ----------
        candles :
            OHLCV DataFrame with at least ``close`` and a DatetimeIndex.

        Returns
        -------
        WFAResults
            Aggregated results with all gates evaluated.
        """
        if len(candles) < self.train_size + self.test_size:
            logger.error(
                "Insufficient data: %d candles, need at least %d",
                len(candles),
                self.train_size + self.test_size,
            )
            return WFAResults()

        windows: List[WFAWindow] = []

        for i in range(self.n_splits):
            test_end = len(candles) - (i * self.test_size)
            test_start = test_end - self.test_size
            train_start = test_start - self.train_size

            if train_start < 0:
                logger.warning("Window %d: insufficient data, skipping", i)
                continue

            train_data = candles.iloc[train_start:test_start]
            test_data = candles.iloc[test_start:test_end]

            logger.info(
                "Window %d: train [%d:%d] (%d rows), test [%d:%d] (%d rows)",
                i,
                train_start,
                test_start,
                len(train_data),
                test_start,
                test_end,
                len(test_data),
            )

            # ---- IS optimisation ----
            best_params, is_metrics = self._optimise(train_data)
            is_sharpe = is_metrics["sharpe"]

            # ---- OOS evaluation ----
            oos_metrics = self._evaluate(test_data, best_params)
            oos_sharpe = oos_metrics["sharpe"]

            window = WFAWindow(
                split_idx=i,
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
                best_params=best_params,
                is_sharpe=is_sharpe,
                oos_sharpe=oos_sharpe,
                is_cagr=is_metrics.get("cagr", 0.0),
                oos_cagr=oos_metrics.get("cagr", 0.0),
                is_maxdd=is_metrics.get("max_drawdown", 0.0),
                oos_maxdd=oos_metrics.get("max_drawdown", 0.0),
            )
            windows.append(window)

            logger.info(
                "Window %d: IS Sharpe=%.3f | OOS Sharpe=%.3f | params=%s",
                i,
                is_sharpe,
                oos_sharpe,
                best_params,
            )

        # ---- Aggregate ----
        oos_sharpes = [w.oos_sharpe for w in windows]
        is_sharpes = [w.is_sharpe for w in windows]

        oos_sharpe_mean = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0
        is_sharpe_mean = float(np.mean(is_sharpes)) if is_sharpes else 0.0

        oos_cagr_mean = float(np.mean([w.oos_cagr for w in windows])) if windows else 0.0
        oos_maxdd_mean = float(np.mean([w.oos_maxdd for w in windows])) if windows else 0.0

        # ---- PBO ----
        pbo = self._calculate_pbo(is_sharpes, oos_sharpes)

        # ---- Deflated Sharpe ----
        n_trials = self._count_param_combinations()
        deflated_sharpe = self._deflated_sharpe(
            oos_sharpe_mean, len(candles), n_trials
        )

        # ---- OOS / IS ratio ----
        oos_is_ratio = (
            oos_sharpe_mean / is_sharpe_mean if is_sharpe_mean != 0 else 0.0
        )

        # ---- Gate check ----
        passes = (
            (deflated_sharpe > DEFLATED_SHARPE_THRESHOLD)
            and (pbo < PBO_THRESHOLD)
            and (oos_is_ratio >= OOS_IS_RATIO_THRESHOLD)
        )

        results = WFAResults(
            windows=windows,
            oos_sharpe=oos_sharpe_mean,
            oos_sharpes=oos_sharpes,
            is_sharpe=is_sharpe_mean,
            is_sharpes=is_sharpes,
            oos_cagr=oos_cagr_mean,
            oos_maxdd=oos_maxdd_mean,
            pbo=pbo,
            deflated_sharpe=deflated_sharpe,
            oos_is_ratio=oos_is_ratio,
            passes_gate=passes,
        )

        logger.info("\n" + results.summary())
        return results

    def run_single_window(
        self,
        train_data: pd.DataFrame,
        test_data: pd.DataFrame,
    ) -> WFAWindow:
        """Run a single train / test window and return results.

        Convenience method for custom WFA schemes.
        """
        best_params, is_metrics = self._optimise(train_data)
        oos_metrics = self._evaluate(test_data, best_params)

        return WFAWindow(
            split_idx=0,
            train_start=0,
            train_end=len(train_data),
            test_start=0,
            test_end=len(test_data),
            best_params=best_params,
            is_sharpe=is_metrics["sharpe"],
            oos_sharpe=oos_metrics["sharpe"],
            is_cagr=is_metrics.get("cagr", 0.0),
            oos_cagr=oos_metrics.get("cagr", 0.0),
            is_maxdd=is_metrics.get("max_drawdown", 0.0),
            oos_maxdd=oos_metrics.get("max_drawdown", 0.0),
        )

    # ------------------------------------------------------------------
    # Internals -- Optimisation
    # ------------------------------------------------------------------

    def _optimise(
        self, train_data: pd.DataFrame
    ) -> Tuple[Dict[str, Any], Dict[str, float]]:
        """Grid-search: find best parameters on *training* data.

        Returns
        -------
        best_params :
            Parameter dict that produced the highest IS Sharpe.
        metrics :
            Dict with ``sharpe``, ``cagr``, ``max_drawdown`` for the best run.
        """
        param_names = list(self.param_grid.keys())
        param_values = list(self.param_grid.values())

        best_sharpe = -999.0
        best_metrics: Dict[str, float] = {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}
        best_params: Dict[str, Any] = {}

        for combo in itertools.product(*param_values):
            params = dict(zip(param_names, combo))

            try:
                metrics = self._backtest(train_data, params)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Param combo failed: %s -- %s", params, exc)
                continue

            if metrics["sharpe"] > best_sharpe:
                best_sharpe = metrics["sharpe"]
                best_metrics = metrics
                best_params = params

        return best_params, best_metrics

    def _evaluate(
        self, test_data: pd.DataFrame, params: Dict[str, Any]
    ) -> Dict[str, float]:
        """Evaluate a *fixed* parameter set on test (OOS) data."""
        try:
            return self._backtest(test_data, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OOS evaluation failed: %s", exc)
            return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}

    # ------------------------------------------------------------------
    # Internals -- Backtest engine
    # ------------------------------------------------------------------

    def _backtest(
        self, data: pd.DataFrame, params: Dict[str, Any]
    ) -> Dict[str, float]:
        """Run a single backtest and return performance metrics.

        Uses vectorbt when available, falls back to a simple PnL vector
        calculation so the module never crashes on import.
        """
        try:
            return self._backtest_vectorbt(data, params)
        except Exception:
            return self._backtest_simple(data, params)

    def _backtest_vectorbt(
        self, data: pd.DataFrame, params: Dict[str, Any]
    ) -> Dict[str, float]:
        """Backtest via ``vectorbt.Portfolio.from_signals``."""
        import vectorbt as vbt

        strategy = self.strategy_class(params=params)
        signals = strategy.generate_signals(data)

        entries = signals["entry"].fillna(False)
        exits = signals["exit"].fillna(False)

        # No signals -> no trades
        if not entries.any() or not exits.any():
            return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}

        portfolio = vbt.Portfolio.from_signals(
            close=data["close"],
            entries=entries,
            exits=exits,
            freq="1D",
            fees=self.fee,
            slippage=self.slippage,
            init_cash=10_000,
        )

        returns = portfolio.returns()
        if len(returns) < 2 or returns.std() == 0:
            return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}

        sharpe = float(
            (returns.mean() / returns.std()) * np.sqrt(self.annualisation_factor)
        )

        # CAGR
        total_return = portfolio.total_return()
        n_years = len(data) / self.annualisation_factor
        cagr = float((1.0 + total_return) ** (1.0 / max(n_years, 1e-6)) - 1.0)

        # Max drawdown
        max_dd = float(portfolio.max_drawdown())

        return {"sharpe": sharpe, "cagr": cagr, "max_drawdown": max_dd}

    def _backtest_simple(
        self, data: pd.DataFrame, params: Dict[str, Any]
    ) -> Dict[str, float]:
        """Fallback backtest using a simple position vector.

        Creates a position vector (+1 long, -1 short, 0 flat) from entry / exit
        signals and computes returns directly.  No dependency on vectorbt.
        """
        strategy = self.strategy_class(params=params)
        signals = strategy.generate_signals(data)

        entries = signals["entry"].fillna(False)
        exits = signals["exit"].fillna(False)

        close = data["close"]
        returns = close.pct_change().dropna()
        if len(returns) < 10:
            return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}

        # Build position vector: +1 after entry, -1 after exit, 0 otherwise
        position = pd.Series(0, index=returns.index, dtype=np.float64)
        in_position = False
        position_side = 0  # +1 long, -1 short

        # Align entry/exit to returns index (drop first NaN)
        entries_aligned = entries.reindex(returns.index, fill_value=False)
        exits_aligned = exits.reindex(returns.index, fill_value=False)

        for i in range(len(returns)):
            if entries_aligned.iloc[i] and not in_position:
                in_position = True
                position_side = 1
            elif exits_aligned.iloc[i] and in_position:
                in_position = False
                position_side = 0
            position.iloc[i] = position_side

        # If never entered a position
        if position.abs().sum() == 0:
            return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0}

        # Strategy returns
        strat_returns = position * returns
        strat_returns = strat_returns - self.fee * position.diff().abs()

        mean_ret = strat_returns.mean()
        std_ret = strat_returns.std()

        sharpe = 0.0
        if std_ret > 0:
            sharpe = float((mean_ret / std_ret) * np.sqrt(self.annualisation_factor))

        # CAGR
        cum = (1.0 + strat_returns).cumprod()
        total_return = float(cum.iloc[-1] - 1.0)
        n_years = len(data) / self.annualisation_factor
        cagr = float((1.0 + total_return) ** (1.0 / max(n_years, 1e-6)) - 1.0)

        # Max drawdown
        peak = cum.cummax()
        drawdown = (cum - peak) / peak
        max_dd = float(drawdown.min())

        return {"sharpe": sharpe, "cagr": cagr, "max_drawdown": max_dd}

    # ------------------------------------------------------------------
    # Internals -- Statistics
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_pbo(
        is_sharpes: List[float], oos_sharpes: List[float]
    ) -> float:
        """Probability of Backtest Overfitting (PBO).

        Uses the CSCV (Combinatorially Symmetric Cross-Validation) logic
        translated to the walk-forward setting:

        For each window we compute the *rank* of the OOS Sharpe relative to
        the IS Sharpe.  PBO is the fraction of windows where the OOS rank is
        in the bottom half (i.e. OOS underperforms IS median).

        This is a practical approximation; the full CSCV algorithm splits the
        full sample into combinatorial halves and ranks every strategy trial.
        Here we only have one optimised strategy per window, so we compare
        each window's OOS outcome to its IS outcome.

        A value near **1.0** means high over-fitting risk.
        A value near **0.0** means the strategy generalises well.
        """
        if not is_sharpes or not oos_sharpes or len(is_sharpes) != len(oos_sharpes):
            return 1.0

        n = len(is_sharpes)
        below_median = 0

        for is_sr, oos_sr in zip(is_sharpes, oos_sharpes):
            # If IS was positive but OOS flipped negative -> over-fit
            if is_sr > 0 and oos_sr < 0:
                below_median += 1
                continue

            # Relative degradation: OOS worse than 50 % of IS level
            if is_sr > 0 and oos_sr < is_sr * 0.5:
                below_median += 1
                continue

            # Both negative means the strategy lost money even IS -> count as fail
            if is_sr < 0 and oos_sr < is_sr:
                below_median += 1
                continue

        return below_median / n

    @staticmethod
    def _deflated_sharpe(
        sharpe: float, n_observations: int, n_trials: int
    ) -> float:
        """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2012).

        Adjusts the observed Sharpe for the fact that we tested many parameter
        combinations and cherry-picked the best one.

        Formula
        -------
        ::

            SR* = SR * sqrt( pi/2 ) / std( max-Sharpe distribution )

        In the simplified closed-form we approximate the variance of the
        maximum of ``n_trials`` independent Sharpe ratios by:

            Var(max SR) ≈ (pi / 2) / n_trials

        which gives:

            SR* ≈ SR / sqrt( n_trials )

        Parameters
        ----------
        sharpe :
            Observed (mean OOS) Sharpe ratio.
        n_observations :
            Total number of candle observations.
        n_trials :
            Number of parameter combinations tested.

        Returns
        -------
        float
            Deflated Sharpe ratio.  Values > 0.95 are considered significant.
        """
        if n_trials <= 1 or n_observations < 2:
            return sharpe

        # Expected maximum Sharpe under the null (Gaussian noise)
        # Euler-Mascheroni constant
        gamma = 0.5772156649
        em_max = (1.0 - gamma) * np.sqrt(n_trials) + gamma / (2.0 * np.sqrt(n_trials))

        # Variance of the maximum
        var_max = (np.pi**2 / 6.0) * (1.0 / n_trials) - (gamma**2) / (4.0 * n_trials)
        var_max = max(var_max, 1e-12)

        # Deflation
        if sharpe <= 0:
            return 0.0

        deflated = (sharpe - em_max) / np.sqrt(var_max)
        return float(deflated)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_param_combinations(self) -> int:
        """Total number of parameter combinations in the grid."""
        import math

        counts = [len(v) for v in self.param_grid.values()]
        return math.prod(counts) if counts else 1


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------
def create_wfa_from_strategy(
    strategy_class: Type[BaseStrategy],
    train_size: int = 252,
    test_size: int = 63,
    n_splits: int = 5,
    **override_grid: Any,
) -> WalkForwardAnalyzer:
    """Create a ``WalkForwardAnalyzer`` from a strategy's parameter ranges.

    The strategy classmethod ``parameter_ranges()`` should return a dict of
    ``param_name -> (min, max, step)`` tuples.  This helper expands those
    ranges into a concrete grid.

    Parameters
    ----------
    strategy_class :
        Strategy class with ``parameter_ranges()`` defined.
    train_size, test_size, n_splits :
        Passed through to ``WalkForwardAnalyzer``.
    override_grid :
        Keyword args to override or extend the auto-generated grid.

    Example
    -------
    >>> wfa = create_wfa_from_strategy(EMACrossoverStrategy, n_splits=5)
    """
    ranges = strategy_class.parameter_ranges()
    grid: Dict[str, List[Any]] = {}

    for param_name, (pmin, pmax, pstep) in ranges.items():
        if isinstance(pmin, float) or isinstance(pstep, float):
            # Float range
            vals = []
            v = pmin
            while v <= pmax + 1e-9:
                vals.append(round(v, 6))
                v += pstep
            grid[param_name] = vals
        else:
            # Integer range
            grid[param_name] = list(range(pmin, pmax + 1, pstep))

    # Apply overrides
    grid.update(override_grid)

    return WalkForwardAnalyzer(
        strategy_class=strategy_class,
        param_grid=grid,
        train_size=train_size,
        test_size=test_size,
        n_splits=n_splits,
    )

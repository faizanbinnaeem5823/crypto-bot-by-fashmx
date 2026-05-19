"""Parameter grid definitions for strategy optimisation.

Each strategy has a predefined searchable parameter space.  Grids are used by
:class:`strategies.walk_forward.WalkForwardAnalyzer` to run walk-forward
optimisation and by hyper-parameter search routines.

To add a new strategy grid:
    1. Create a new ``Dict[str, List]`` constant below.
    2. Register it in ``STRATEGY_GRIDS``.
    3. Optionally expose a helper in :func:`get_grid`.

Usage::

    from strategies.parameter_grid import get_grid, EMA_CROSSOVER_GRID

    grid = get_grid("ema_crossover")
    #  {"fast": [5, 8, 9, 10, 12], "slow": [21, 26, 30, 50]}

    # Or use the constant directly
    grid = EMA_CROSSOVER_GRID
"""

from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# EMA Crossover
# ---------------------------------------------------------------------------
EMA_CROSSOVER_GRID: Dict[str, List[int]] = {
    "fast": [5, 8, 9, 10, 12, 15],
    "slow": [21, 26, 30, 50, 55],
}

# ---------------------------------------------------------------------------
# RSI + MACD Combo
# ---------------------------------------------------------------------------
RSI_MACD_GRID: Dict[str, List[Any]] = {
    "rsi_period": [7, 10, 14, 20],
    "oversold": [30, 35, 40, 45],
    "overbought": [55, 60, 65, 70],
    "macd_fast": [8, 12, 15],
    "macd_slow": [21, 26, 30],
    "macd_signal": [7, 9, 12],
}

# ---------------------------------------------------------------------------
# Grid registry
# ---------------------------------------------------------------------------
STRATEGY_GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "ema_crossover": EMA_CROSSOVER_GRID,
    "rsi_macd": RSI_MACD_GRID,
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def get_grid(strategy_name: str) -> Dict[str, List[Any]]:
    """Return the parameter grid for a named strategy.

    Parameters
    ----------
    strategy_name :
        Strategy key, e.g. ``"ema_crossover"`` or ``"rsi_macd"``.

    Returns
    -------
    dict
        Parameter name -> list of candidate values.  Returns an empty dict
        if the strategy is not registered.
    """
    grid = STRATEGY_GRIDS.get(strategy_name, {})
    if not grid:
        raise ValueError(
            f"Unknown strategy '{strategy_name}'. "
            f"Available: {list(STRATEGY_GRIDS.keys())}"
        )
    return grid


def list_strategies() -> List[str]:
    """Return the list of strategy names that have registered grids."""
    return list(STRATEGY_GRIDS.keys())


def register_grid(
    strategy_name: str, grid: Dict[str, List[Any]]
) -> None:
    """Register a new parameter grid at runtime.

    Parameters
    ----------
    strategy_name :
        Key used to look up the grid later.
    grid :
        Parameter name -> list of candidate values.
    """
    STRATEGY_GRIDS[strategy_name] = grid


def total_combinations(strategy_name: str) -> int:
    """Return the number of parameter combinations for a strategy grid.

    Useful for estimating optimisation run-time.
    """
    import math

    grid = get_grid(strategy_name)
    counts = [len(v) for v in grid.values()]
    return math.prod(counts) if counts else 0

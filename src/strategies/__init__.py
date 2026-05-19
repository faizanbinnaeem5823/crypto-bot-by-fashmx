"""CryptoBot strategies package."""

__version__ = "0.1.0"

# Walk-forward analysis (strategy validation)
from .walk_forward import WalkForwardAnalyzer, WFAResults, WFAWindow
from .parameter_grid import get_grid, list_strategies, total_combinations
from .performance_tracker import PerformanceTracker, StrategyPerformance

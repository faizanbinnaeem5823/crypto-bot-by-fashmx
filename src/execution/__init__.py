"""
Crypto Trading Bot - Execution Module
======================================

Handles trade execution and order management:
    - Market and limit order placement
    - Position sizing (fixed, percentage, Kelly)
    - Slippage estimation and tracking
    - Order status monitoring and callbacks
    - Paper-trading / backtest execution engine

All monetary values use Decimal for precision.
"""

import logging

logger = logging.getLogger(__name__)

__version__ = "0.1.0"
__all__ = [
    "OrderExecutor",
    "PositionSizer",
    "PaperTrader",
    "ExecutionCallback",
]

# Deferred imports
# from .executor import OrderExecutor
# from .sizing import PositionSizer
# from .paper import PaperTrader

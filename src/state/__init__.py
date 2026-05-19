"""
Crypto Trading Bot – State Module
==================================

Persistent state management using DuckDB with resilience retries (R6)
and daily P&L reset tracking (R8).
"""

__version__ = "0.1.0"

from .state_manager import StateManager, StateManagerError, Trade
from .cross_bot_state import CrossBotState

__all__ = ["StateManager", "Trade", "CrossBotState", "StateManagerError"]

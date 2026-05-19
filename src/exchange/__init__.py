"""
Crypto Trading Bot - Exchange Module
=====================================

Provides exchange connectivity via CCXT for:
    - Unified market data (OHLCV, order book, ticker)
    - Order management (create, cancel, query)
    - Account balance queries
    - Rate-limit-aware request handling

Currently supported: Binance (spot & futures).
Pluggable architecture for adding additional exchanges.
"""

import logging

logger = logging.getLogger(__name__)

__version__ = "0.1.0"
__all__ = [
    "BinanceExchange",
    "ExchangeBase",
    "get_exchange",
]

# Deferred imports to avoid circular dependencies
# from .binance import BinanceExchange
# from .base import ExchangeBase, get_exchange

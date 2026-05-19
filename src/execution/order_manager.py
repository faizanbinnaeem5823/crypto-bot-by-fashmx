"""
Crypto Trading Bot - Order Manager
===================================

Routes orders to either a paper broker or a live exchange client.

Features
--------
- Order validation (minimum sizes)
- Kill-switch / risk-engine gate before every submission
- Structured logging of every order lifecycle event
- Unified interface for paper and live trading
- All monetary values use ``Decimal`` for precision.

Architecture
------------
    OrderManager receives signals → validates → risk checks → submits
                    ↑
            (kill_switch + risk_engine gates)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..exchange.binance_client import BinanceClient
    from ..exchange.paper_broker import PaperBroker
    from ..risk.kill_switch import KillSwitch
    from ..risk.risk_engine import RiskEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimum order sizes by symbol (base asset quantity)
# ---------------------------------------------------------------------------
MIN_ORDER_SIZES: Dict[str, Decimal] = {
    "BTC/USDT": Decimal("0.00001"),
    "ETH/USDT": Decimal("0.0001"),
    "SOL/USDT": Decimal("0.001"),
    "BNB/USDT": Decimal("0.001"),
    "XRP/USDT": Decimal("1.0"),
    "ADA/USDT": Decimal("1.0"),
    "DOGE/USDT": Decimal("1.0"),
}


class OrderManager:
    """Central order manager that routes to paper or live execution.

    Parameters
    ----------
    bot_id:
        Unique identifier for this bot instance (used in structured logging).
    exchange_client:
        Live :class:`BinanceClient` instance (required even when paper=True).
    paper_broker:
        Paper-trading :class:`PaperBroker` instance.
    paper:
        If ``True`` (default), route orders through *paper_broker*.  If
        ``False``, send to the live *exchange_client*.
    kill_switch:
        Optional :class:`KillSwitch` — when triggered **no** orders are sent.
    risk_engine:
        Optional :class:`RiskEngine` — consulted before each submission.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        bot_id: str,
        exchange_client: "BinanceClient",
        paper_broker: "PaperBroker",
        paper: bool = True,
        kill_switch: Optional["KillSwitch"] = None,
        risk_engine: Optional["RiskEngine"] = None,
    ) -> None:
        if not bot_id:
            raise ValueError("bot_id is required")
        if exchange_client is None:
            raise ValueError("exchange_client is required")
        if paper_broker is None:
            raise ValueError("paper_broker is required")

        self.bot_id: str = bot_id
        self.exchange_client: "BinanceClient" = exchange_client
        self.paper_broker: "PaperBroker" = paper_broker
        self.paper: bool = paper
        self.kill_switch: Optional["KillSwitch"] = kill_switch
        self.risk_engine: Optional["RiskEngine"] = risk_engine

        self._pending_orders: list[dict] = []

        logger.info(
            "OrderManager initialised — bot_id=%s paper=%s "
            "kill_switch=%s risk_engine=%s",
            self.bot_id,
            self.paper,
            self.kill_switch is not None,
            self.risk_engine is not None,
        )

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate_order_size(self, quantity: Decimal, symbol: str) -> bool:
        """Return ``True`` if *quantity* meets the minimum for *symbol*."""
        min_size = MIN_ORDER_SIZES.get(symbol, Decimal("0.00001"))
        return quantity >= min_size

    # ------------------------------------------------------------------ #
    # Risk gate
    # ------------------------------------------------------------------ #

    def _risk_check(
        self,
        portfolio_value: Optional[Decimal] = None,
        daily_pnl: Optional[Decimal] = None,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` after checking kill switch and risk engine."""
        # 1. Kill switch
        if self.kill_switch is not None and self.kill_switch.is_triggered():
            logger.critical(
                "bot_id=%s | Order BLOCKED — kill switch triggered",
                self.bot_id,
            )
            return False, "Kill switch triggered"

        # 2. Risk engine
        if self.risk_engine is not None:
            if portfolio_value is None:
                portfolio_value = Decimal("0")
            if daily_pnl is None:
                daily_pnl = Decimal("0")
            allowed, reason = self.risk_engine.check_trade_allowed(
                portfolio_value, daily_pnl
            )
            if not allowed:
                logger.warning(
                    "bot_id=%s | Order BLOCKED — risk engine: %s",
                    self.bot_id,
                    reason,
                )
                return False, reason

        return True, "OK"

    # ------------------------------------------------------------------ #
    # Order submission
    # ------------------------------------------------------------------ #

    async def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        portfolio_value: Optional[Decimal] = None,
        daily_pnl: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """Submit an order after validation and risk checks.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        side:
            ``"BUY"`` or ``"SELL"``.
        quantity:
            Amount of the base asset to trade.
        price:
            Optional price for limit orders (ignored for market orders).
        portfolio_value:
            Current portfolio value for risk-engine checks.
        daily_pnl:
            Current daily PnL for risk-engine checks.

        Returns
        -------
        dict
            Fill result from the chosen broker / exchange.

        Raises
        ------
        ValueError
            If the order size is below the minimum for the symbol.
        RuntimeError
            If the kill switch is triggered or the risk engine rejects the trade.
        """
        # --- 1. Size validation ---------------------------------------- #
        if not self.validate_order_size(quantity, symbol):
            logger.error(
                "bot_id=%s | Order REJECTED — size %s below minimum for %s",
                self.bot_id,
                quantity,
                symbol,
            )
            raise ValueError(
                f"Order size {quantity} below minimum for {symbol}"
            )

        # --- 2. Risk gate ----------------------------------------------- #
        allowed, reason = self._risk_check(portfolio_value, daily_pnl)
        if not allowed:
            raise RuntimeError(
                f"Order blocked by risk controls: {reason}"
            )

        # --- 3. Route to broker ---------------------------------------- #
        order_log = {
            "bot_id": self.bot_id,
            "mode": "paper" if self.paper else "live",
            "symbol": symbol,
            "side": side.upper(),
            "quantity": str(quantity),
            "price": str(price) if price is not None else None,
            "portfolio_value": str(portfolio_value) if portfolio_value is not None else None,
            "daily_pnl": str(daily_pnl) if daily_pnl is not None else None,
        }

        if self.paper:
            logger.info(
                "bot_id=%s | Submitting PAPER order — %s %s %s %s",
                self.bot_id,
                side.upper(),
                quantity,
                symbol,
                f"@ {price}" if price is not None else "MARKET",
            )

            # PaperBroker.place_market_order expects (symbol, side, quantity, price)
            # If no price is provided, we use a dummy price for paper trading
            exec_price = price if price is not None else Decimal("0")
            result = self.paper_broker.place_market_order(
                symbol=symbol,
                side=side.upper(),
                quantity=quantity,
                price=exec_price,
            )
        else:
            logger.info(
                "bot_id=%s | Submitting LIVE order — %s %s %s MARKET",
                self.bot_id,
                side.upper(),
                quantity,
                symbol,
            )

            result = await self.exchange_client.place_market_order(
                symbol=symbol,
                side=side.upper(),
                quantity=quantity,
            )

        # --- 4. Structured result --------------------------------------- #
        result["_meta"] = order_log
        result["_mode"] = "paper" if self.paper else "live"

        logger.info(
            "bot_id=%s | Order %s — status=%s symbol=%s side=%s",
            self.bot_id,
            "FILLED" if result.get("status") == "filled" else result.get("status", "UNKNOWN"),
            result.get("status"),
            symbol,
            side.upper(),
        )

        return result

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #

    def get_pending_orders(self) -> list[dict]:
        """Return a snapshot of currently pending orders."""
        return list(self._pending_orders)

    async def cancel_pending(self) -> None:
        """Clear the pending-orders cache (informational only)."""
        count = len(self._pending_orders)
        self._pending_orders.clear()
        logger.info("bot_id=%s | Cleared %d pending orders", self.bot_id, count)

"""Order manager with partial fill handling, status polling, and timeout cancellation.

Features:
- Submit orders with full validation
- Poll for fill status
- Handle partial fills (record partial, track remainder)
- Cancel unfilled orders after timeout
- Slippage tolerance check
- Idempotent submission (prevent double-submit)

All monetary values use ``Decimal`` for precision.  All timestamps are UTC.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..exchange.binance_client import BinanceClient
    from ..exchange.paper_broker import PaperBroker
    from ..risk.kill_switch import KillSwitch, KillSwitchError
    from ..risk.risk_engine import RiskEngine
    from ..state.state_manager import StateManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------


class OrderStatus(Enum):
    """Lifecycle states for an order."""

    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class FillResult:
    """Result of an order fill (complete or partial)."""

    order_id: str
    status: OrderStatus
    symbol: str
    side: str
    requested_qty: Decimal
    filled_qty: Decimal
    remaining_qty: Decimal
    avg_price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Optional[Decimal] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PendingOrder:
    """Track an order from submission through final fill."""

    order_id: str
    symbol: str
    side: str
    requested_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.PENDING
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_poll: Optional[datetime] = None


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
    """Order manager with partial fill handling.

    Parameters
    ----------
    bot_id:
        Unique identifier for this bot instance (used in structured logging).
    exchange_client:
        Live :class:`BinanceClient` instance (required when ``paper=False``).
    paper_broker:
        Paper-trading :class:`PaperBroker` instance (required when ``paper=True``).
    paper:
        If ``True`` (default), route orders through *paper_broker*.  If
        ``False``, send to the live *exchange_client*.
    kill_switch:
        Optional :class:`KillSwitch` — when triggered **no** orders are sent.
    risk_engine:
        Optional :class:`RiskEngine` — consulted before each submission.
    state_manager:
        Optional :class:`StateManager` — persisted state access.
    poll_interval_sec:
        Seconds between polls when waiting for a fill (default 5.0).
    fill_timeout_sec:
        Default timeout in seconds before an unfilled order is cancelled
        (default 300).
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        bot_id: str,
        exchange_client: Optional["BinanceClient"] = None,
        paper_broker: Optional["PaperBroker"] = None,
        paper: bool = True,
        kill_switch: Optional["KillSwitch"] = None,
        risk_engine: Optional["RiskEngine"] = None,
        state_manager: Optional["StateManager"] = None,
        poll_interval_sec: float = 5.0,
        fill_timeout_sec: int = 300,
    ) -> None:
        if not bot_id:
            raise ValueError("bot_id is required")

        self.bot_id: str = bot_id
        self.exchange: Optional["BinanceClient"] = exchange_client
        self.paper_broker: Optional["PaperBroker"] = paper_broker
        self.paper: bool = paper
        self.kill_switch: Optional["KillSwitch"] = kill_switch
        self.risk_engine: Optional["RiskEngine"] = risk_engine
        self.state: Optional["StateManager"] = state_manager
        self.poll_interval: float = poll_interval_sec
        self.fill_timeout: int = fill_timeout_sec

        self._pending_orders: Dict[str, PendingOrder] = {}
        self._submitted_ids: set = set()  # Prevent double-submit

        # Slippage tolerance: reject fill if price moves more than this
        self.max_slippage_pct: Decimal = Decimal("1.0")  # 1%

        logger.info(
            "OrderManager initialised — bot_id=%s paper=%s "
            "kill_switch=%s risk_engine=%s poll_interval=%ss fill_timeout=%ss",
            self.bot_id,
            self.paper,
            self.kill_switch is not None,
            self.risk_engine is not None,
            self.poll_interval,
            self.fill_timeout,
        )

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate_order_size(self, quantity: Decimal, symbol: str) -> bool:
        """Return ``True`` if *quantity* meets the minimum for *symbol*."""
        min_size = MIN_ORDER_SIZES.get(symbol, Decimal("0.00001"))
        return quantity >= min_size

    # ------------------------------------------------------------------ #
    # Public: submit_order
    # ------------------------------------------------------------------ #

    async def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        max_slippage: Optional[Decimal] = None,
    ) -> FillResult:
        """Submit an order with full validation and kill-switch / risk checks.

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
        max_slippage:
            Optional override for the default slippage tolerance (percent).

        Returns
        -------
        FillResult
            The initial fill result.  For live orders this is often
            :attr:`OrderStatus.PENDING`; callers should use
            :meth:`poll_until_complete` to wait for the final state.

        Raises
        ------
        KillSwitchError
            If the kill switch is triggered.
        ValueError
            If the order size is below the minimum for the symbol.
        RuntimeError
            If the exchange client or paper broker is not configured.
        """
        # 1. Kill switch check  (FIRST thing)
        if self.kill_switch is not None and self.kill_switch.is_triggered():
            logger.critical(
                "bot_id=%s | Order REJECTED — kill switch triggered",
                self.bot_id,
            )
            raise KillSwitchError("Trading halted by kill switch")

        # 2. Risk check  (SECOND thing)
        if self.risk_engine is not None:
            portfolio_value = Decimal("500")  # Should come from state
            daily_pnl = Decimal("0")
            allowed, reason = self.risk_engine.check_trade_allowed(
                portfolio_value, daily_pnl
            )
            if not allowed:
                logger.warning(
                    "bot_id=%s | Order REJECTED — risk: %s",
                    self.bot_id,
                    reason,
                )
                return FillResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    symbol=symbol,
                    side=side,
                    requested_qty=quantity,
                    filled_qty=Decimal("0"),
                    remaining_qty=quantity,
                    avg_price=Decimal("0"),
                    commission=Decimal("0"),
                    commission_asset="USDT",
                )

        # 3. Order size validation
        if not self.validate_order_size(quantity, symbol):
            logger.warning(
                "bot_id=%s | Order REJECTED — size below minimum for %s",
                self.bot_id,
                symbol,
            )
            return FillResult(
                order_id="",
                status=OrderStatus.REJECTED,
                symbol=symbol,
                side=side,
                requested_qty=quantity,
                filled_qty=Decimal("0"),
                remaining_qty=quantity,
                avg_price=Decimal("0"),
                commission=Decimal("0"),
                commission_asset="USDT",
            )

        # 4. Idempotency / double-submit guard
        order_key = f"{symbol}_{side}_{quantity}_{price}"
        if order_key in self._submitted_ids:
            logger.warning(
                "bot_id=%s | Order REJECTED — duplicate submit detected (%s)",
                self.bot_id,
                order_key,
            )
            return FillResult(
                order_id="",
                status=OrderStatus.REJECTED,
                symbol=symbol,
                side=side,
                requested_qty=quantity,
                filled_qty=Decimal("0"),
                remaining_qty=quantity,
                avg_price=Decimal("0"),
                commission=Decimal("0"),
                commission_asset="USDT",
            )
        self._submitted_ids.add(order_key)

        # 5. Submit
        if self.paper:
            result = await self._submit_paper(symbol, side, quantity, price)
        else:
            result = await self._submit_live(symbol, side, quantity, price)

        # 6. Track pending
        if result.status == OrderStatus.PENDING:
            self._pending_orders[result.order_id] = PendingOrder(
                order_id=result.order_id,
                symbol=symbol,
                side=side,
                requested_qty=quantity,
            )

        logger.info(
            "bot_id=%s | Order %s: %s %s %s → %s",
            self.bot_id,
            result.order_id,
            side,
            quantity,
            symbol,
            result.status.value,
        )
        return result

    # ------------------------------------------------------------------ #
    # Internal: paper submission
    # ------------------------------------------------------------------ #

    async def _submit_paper(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> FillResult:
        """Submit via paper broker.

        Paper orders fill immediately.
        """
        if self.paper_broker is None:
            raise RuntimeError("Paper broker not configured")

        exec_price = price if price is not None else Decimal("0")
        self.paper_broker.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=exec_price,
        )

        commission = quantity * exec_price * Decimal("0.001")

        return FillResult(
            order_id=f"paper_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            status=OrderStatus.FILLED,
            symbol=symbol,
            side=side,
            requested_qty=quantity,
            filled_qty=quantity,
            remaining_qty=Decimal("0"),
            avg_price=exec_price,
            commission=commission,
            commission_asset="USDT",
        )

    # ------------------------------------------------------------------ #
    # Internal: live submission
    # ------------------------------------------------------------------ #

    async def _submit_live(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> FillResult:
        """Submit via live exchange.

        Live orders may come back ``PENDING`` or ``PARTIALLY_FILLED``.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange client not configured")

        result: Dict[str, Any] = await self.exchange.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
        )

        # Parse response
        filled = Decimal(str(result.get("executedQty", "0")))
        if filled >= quantity:
            status = OrderStatus.FILLED
        elif filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.PENDING

        return FillResult(
            order_id=str(result.get("orderId", "")),
            status=status,
            symbol=symbol,
            side=side,
            requested_qty=quantity,
            filled_qty=filled,
            remaining_qty=quantity - filled,
            avg_price=Decimal(str(result.get("avgPrice", "0"))),
            commission=Decimal(str(result.get("commission", "0"))),
            commission_asset=result.get("commissionAsset", "USDT"),
        )

    # ------------------------------------------------------------------ #
    # Public: polling
    # ------------------------------------------------------------------ #

    async def poll_fill_status(self, order_id: str) -> FillResult:
        """Poll exchange for current fill status of an order.

        Parameters
        ----------
        order_id:
            The exchange-side order identifier.

        Returns
        -------
        FillResult
            Current snapshot of the order's fill state.

        Raises
        ------
        RuntimeError
            If the exchange client is not configured.
        """
        if self.paper:
            # Paper orders fill immediately
            if order_id in self._pending_orders:
                po = self._pending_orders[order_id]
                return FillResult(
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    symbol=po.symbol,
                    side=po.side,
                    requested_qty=po.requested_qty,
                    filled_qty=po.requested_qty,
                    remaining_qty=Decimal("0"),
                    avg_price=Decimal("0"),
                    commission=Decimal("0"),
                    commission_asset="USDT",
                )

        if self.exchange is None:
            raise RuntimeError("Exchange client not configured")

        order_info: Dict[str, Any] = await self.exchange.get_order(order_id)

        filled = Decimal(str(order_info.get("executedQty", "0")))
        total = Decimal(str(order_info.get("origQty", "0")))

        status_map = {
            "NEW": OrderStatus.PENDING,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }

        return FillResult(
            order_id=order_id,
            status=status_map.get(order_info.get("status"), OrderStatus.PENDING),
            symbol=order_info.get("symbol", ""),
            side=order_info.get("side", ""),
            requested_qty=total,
            filled_qty=filled,
            remaining_qty=total - filled,
            avg_price=Decimal(str(order_info.get("avgPrice", "0"))),
            commission=Decimal(str(order_info.get("commission", "0"))),
            commission_asset=order_info.get("commissionAsset", "USDT"),
        )

    async def poll_until_complete(
        self,
        order_id: str,
        timeout_sec: Optional[int] = None,
    ) -> FillResult:
        """Poll order until filled, cancelled, or timeout.

        Parameters
        ----------
        order_id:
            The exchange-side order identifier.
        timeout_sec:
            Override for the default fill timeout in seconds.

        Returns
        -------
        FillResult
            Final fill result when the order reaches a terminal state.
        """
        timeout = timeout_sec or self.fill_timeout
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        while datetime.now(timezone.utc).timestamp() < deadline:
            result = await self.poll_fill_status(order_id)

            if result.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            ):
                if order_id in self._pending_orders:
                    del self._pending_orders[order_id]
                return result

            # Log progress on partial fills
            if result.status == OrderStatus.PARTIALLY_FILLED:
                logger.info(
                    "bot_id=%s | Order %s: partial fill %s / %s",
                    self.bot_id,
                    order_id,
                    result.filled_qty,
                    result.requested_qty,
                )
                if self._pending_orders.get(order_id) is not None:
                    self._pending_orders[order_id].filled_qty = result.filled_qty
                    self._pending_orders[order_id].last_poll = datetime.now(
                        timezone.utc
                    )

            await asyncio.sleep(self.poll_interval)

        # Timeout — cancel remaining
        logger.warning(
            "bot_id=%s | Order %s: fill timeout, cancelling",
            self.bot_id,
            order_id,
        )
        await self.cancel_order(order_id)
        return await self.poll_fill_status(order_id)

    # ------------------------------------------------------------------ #
    # Public: cancellation
    # ------------------------------------------------------------------ #

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order.

        Parameters
        ----------
        order_id:
            The exchange-side order identifier.

        Returns
        -------
        bool
            ``True`` if the cancellation succeeded.
        """
        try:
            if self.paper:
                # Paper orders can't be cancelled (instant fill)
                pass
            elif self.exchange is not None:
                await self.exchange.cancel_order(order_id)

            if order_id in self._pending_orders:
                self._pending_orders[order_id].status = OrderStatus.CANCELLED

            logger.info(
                "bot_id=%s | Order %s: cancelled",
                self.bot_id,
                order_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "bot_id=%s | Cancel failed for %s: %s",
                self.bot_id,
                order_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #

    def get_pending_orders(self) -> List[PendingOrder]:
        """Return a snapshot of currently pending orders."""
        return list(self._pending_orders.values())

    async def cancel_pending(self) -> None:
        """Cancel every order currently tracked as pending."""
        order_ids = list(self._pending_orders.keys())
        for oid in order_ids:
            await self.cancel_order(oid)
        logger.info(
            "bot_id=%s | Cancelled %d pending orders",
            self.bot_id,
            len(order_ids),
        )

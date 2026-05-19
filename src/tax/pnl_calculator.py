"""P&L calculator for crypto trades."""

import logging
from decimal import Decimal
from typing import List, Dict

logger = logging.getLogger(__name__)


class PnLCalculator:
    """Calculate realized and unrealized P&L.

    Uses FIFO method for cost basis calculation.
    """

    def __init__(self):
        self.positions = {}  # symbol -> list of (quantity, cost_basis)

    def record_buy(self, symbol: str, quantity: Decimal, price: Decimal):
        """Record a buy — adds to position stack."""
        if symbol not in self.positions:
            self.positions[symbol] = []
        self.positions[symbol].append((quantity, price))

    def record_sell(self, symbol: str, quantity: Decimal, price: Decimal) -> Decimal:
        """Record a sell — calculates P&L using FIFO.

        Returns realized P&L.
        """
        if symbol not in self.positions or not self.positions[symbol]:
            return Decimal("0")

        remaining = quantity
        total_pnl = Decimal("0")

        while remaining > 0 and self.positions[symbol]:
            pos_qty, pos_price = self.positions[symbol][0]

            if pos_qty <= remaining:
                # Sell entire position lot
                pnl = pos_qty * (price - pos_price)
                total_pnl += pnl
                remaining -= pos_qty
                self.positions[symbol].pop(0)
            else:
                # Partial sell
                pnl = remaining * (price - pos_price)
                total_pnl += pnl
                self.positions[symbol][0] = (pos_qty - remaining, pos_price)
                remaining = Decimal("0")

        return total_pnl

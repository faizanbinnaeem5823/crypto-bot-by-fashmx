import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

logger = logging.getLogger(__name__)

class PaperBroker:
    def __init__(self, initial_balance: Decimal = Decimal("500")):
        self.balances: Dict[str, Decimal] = {"USDT": initial_balance}
        self.positions: Dict[str, Decimal] = {}
        self.trade_history = []

    def get_balance(self, asset: str) -> Decimal:
        return self.balances.get(asset, Decimal("0"))

    def place_market_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> dict:
        base, quote = symbol.split("/")
        cost = quantity * price
        if side == "BUY":
            self.balances[quote] = self.balances.get(quote, Decimal("0")) - cost
            self.positions[base] = self.positions.get(base, Decimal("0")) + quantity
        else:
            self.positions[base] = self.positions.get(base, Decimal("0")) - quantity
            self.balances[quote] = self.balances.get(quote, Decimal("0")) + cost

        trade = {"status": "filled", "symbol": symbol, "side": side,
                 "quantity": str(quantity), "price": str(price),
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        self.trade_history.append(trade)
        return trade

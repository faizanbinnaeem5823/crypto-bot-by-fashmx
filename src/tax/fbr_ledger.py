"""FBR-compliant consolidated ledger for Pakistan tax reporting.

Requirements:
- All transactions in PKR (converted from USD at SBP rate)
- bot_id tag on every transaction
- Timestamp in Pakistan timezone (UTC+5)
- Fee breakdown separate
"""

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

# Approximate USD to PKR rate (should be fetched from API in production)
USD_TO_PKR = Decimal("278.50")
PAK_TZ = timezone(timedelta(hours=5))


class FBRLedger:
    """FBR-compliant transaction ledger.

    Usage:
        ledger = FBRLedger()
        entry = ledger.record_trade(trade_dict)
        ledger.export("data/fbr_ledger_2025.csv")
    """

    def __init__(self, bot_id: str = "bot_a"):
        self.bot_id = bot_id
        self.entries = []

    def record_trade(self, trade: Dict) -> Dict:
        """Record a trade in FBR format."""
        symbol = trade.get("symbol", "BTC/USDT")
        base, quote = symbol.split("/")
        side = trade.get("side", "BUY")
        quantity = Decimal(str(trade.get("quantity", 0)))
        price_usd = Decimal(str(trade.get("price", 0)))
        value_usd = quantity * price_usd
        value_pkr = value_usd * USD_TO_PKR
        fee_usd = value_usd * Decimal("0.001")
        fee_pkr = fee_usd * USD_TO_PKR

        timestamp = trade.get("timestamp", datetime.now(timezone.utc))
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

        entry = {
            "date_utc": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "date_pkr": timestamp.astimezone(PAK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "bot_id": self.bot_id,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price_usd": str(price_usd),
            "value_usd": str(value_usd),
            "value_pkr": str(value_pkr),
            "fee_usd": str(fee_usd),
            "fee_pkr": str(fee_pkr),
            "net_value_pkr": str(value_pkr - fee_pkr if side == "SELL" else -(value_pkr + fee_pkr)),
        }
        self.entries.append(entry)
        return entry

    def export(self, output_path: str):
        """Export ledger to CSV."""
        df = pd.DataFrame(self.entries)
        df.to_csv(output_path, index=False)
        logger.info(f"Exported {len(self.entries)} FBR entries to {output_path}")
        return output_path

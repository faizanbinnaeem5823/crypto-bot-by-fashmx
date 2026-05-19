"""Koinly tax export: converts bot trades to Koinly-compatible CSV.

Koinly CSV columns:
- Date (UTC)
- Sent Amount, Sent Currency
- Received Amount, Received Currency
- Fee Amount, Fee Currency
- Net Worth Amount, Net Worth Currency
- Label (optional)
- Description (optional)
- TxHash (optional)
"""

import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Dict

import pandas as pd

logger = logging.getLogger(__name__)


class KoinlyExporter:
    """Export trades to Koinly CSV format.

    Usage:
        exporter = KoinlyExporter()
        exporter.export(trades, "data/koinly_export_2025.csv")
    """

    def __init__(self, bot_id: str = "bot_a"):
        self.bot_id = bot_id

    def export(self, trades: List[Dict], output_path: str):
        """Export trades to Koinly CSV."""
        rows = []
        for trade in trades:
            row = self._convert_trade(trade)
            rows.append(row)

        df = pd.DataFrame(rows)
        df.to_csv(output_path, index=False)
        logger.info(f"Exported {len(rows)} trades to {output_path}")
        return output_path

    def _convert_trade(self, trade: Dict) -> Dict:
        """Convert a single trade to Koinly format."""
        symbol = trade.get("symbol", "BTC/USDT")
        base, quote = symbol.split("/")
        side = trade.get("side", "BUY")
        quantity = Decimal(str(trade.get("quantity", 0)))
        price = Decimal(str(trade.get("price", 0)))
        fee = quantity * price * Decimal("0.001")  # 0.1% fee
        timestamp = trade.get("timestamp", datetime.now(timezone.utc))

        if side == "BUY":
            return {
                "Date": timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(timestamp, datetime) else timestamp,
                "Sent Amount": str(quantity * price),
                "Sent Currency": quote,
                "Received Amount": str(quantity),
                "Received Currency": base,
                "Fee Amount": str(fee),
                "Fee Currency": quote,
                "Net Worth Amount": "",
                "Net Worth Currency": "",
                "Label": "",
                "Description": f"{self.bot_id} {side} {symbol}",
                "TxHash": "",
            }
        else:
            return {
                "Date": timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(timestamp, datetime) else timestamp,
                "Sent Amount": str(quantity),
                "Sent Currency": base,
                "Received Amount": str(quantity * price),
                "Received Currency": quote,
                "Fee Amount": str(fee),
                "Fee Currency": quote,
                "Net Worth Amount": "",
                "Net Worth Currency": "",
                "Label": "",
                "Description": f"{self.bot_id} {side} {symbol}",
                "TxHash": "",
            }

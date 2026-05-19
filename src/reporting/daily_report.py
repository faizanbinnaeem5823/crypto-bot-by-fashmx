"""Daily and weekly automated report generation.

Generates reports from StateManager data and exports as text/Markdown.
Can be sent via alerts system.
"""

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd
from tabulate import tabulate

from state.state_manager import StateManager

logger = logging.getLogger(__name__)


class DailyReport:
    """Generate daily trading reports.

    Usage:
        report = DailyReport(state_manager, bot_id="bot_a")
        summary = report.generate_daily()
        print(summary)
    """

    def __init__(self, state_manager: StateManager, bot_id: str):
        self.state = state_manager
        self.bot_id = bot_id

    def generate_daily(self) -> str:
        """Generate daily summary report."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Get today's trades
        trades = self.state.get_recent_trades(limit=1000)
        today_trades = [t for t in trades if t.get("timestamp", datetime.min) >= today_start]

        # Calculate metrics
        total_trades = len(today_trades)
        winning_trades = sum(1 for t in today_trades if (t.get("pnl") or 0) > 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum((t.get("pnl") or 0) for t in today_trades)

        # Equity curve
        equity = self.state.get_equity_curve(limit=2)
        start_equity = equity[-1]["equity"] if len(equity) >= 2 else 500
        end_equity = equity[0]["equity"] if equity else 500

        lines = [
            f"📊 Daily Report — {self.bot_id} — {now.strftime('%Y-%m-%d')}",
            f"Timeframe: UTC | Generated: {now.strftime('%H:%M:%S')}",
            "",
            "═══ Performance ═══",
            f"Total Trades:    {total_trades}",
            f"Winning Trades:  {winning_trades}",
            f"Win Rate:        {win_rate:.1f}%",
            f"Total P&L:       ${total_pnl:+.2f}",
            f"Start Equity:    ${start_equity:.2f}",
            f"End Equity:      ${end_equity:.2f}",
            f"Return:          {((end_equity/start_equity-1)*100):+.2f}%",
            "",
            "═══ Trades ═══",
        ]

        if today_trades:
            for t in today_trades[:10]:
                pnl = t.get("pnl", 0)
                pnl_str = f"${pnl:+.2f}" if pnl else "N/A"
                lines.append(f"  {t.get('side')} {t.get('symbol')} @ {t.get('price')} | P&L: {pnl_str}")
        else:
            lines.append("  No trades today")

        return "\n".join(lines)

    def check_retirement_conditions(self, sharpe: float, drawdown_pct: float,
                                     losing_months: int, overrides_30d: int,
                                     drift_hours: float) -> tuple[bool, str]:
        """Check if bot should auto-retire. Returns (should_retire, reason).

        Retirement triggers:
        - Sharpe < 0
        - Max drawdown > 20%
        - 3 consecutive losing months
        - > 5 overrides per 30 days
        - Drift > 24 hours
        """
        if sharpe < 0:
            return True, f"Sharpe {sharpe:.2f} < 0"
        if drawdown_pct > 20:
            return True, f"Drawdown {drawdown_pct:.1f}% > 20%"
        if losing_months >= 3:
            return True, f"{losing_months} consecutive losing months"
        if overrides_30d > 5:
            return True, f"{overrides_30d} overrides in 30 days > 5"
        if drift_hours > 24:
            return True, f"Drift {drift_hours:.1f}h > 24h"
        return False, ""

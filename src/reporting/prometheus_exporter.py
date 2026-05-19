"""Prometheus metrics exporter for the trading bot.

Exports:
- bot_equity: Current portfolio equity in USD
- bot_drawdown: Current drawdown percentage
- bot_daily_pnl: Daily P&L in USD
- bot_win_rate: Win rate percentage (trailing 30 days)
- bot_total_trades: Total trade count
- bot_open_positions: Number of open positions
- bot_signal_strength: Last signal strength (0-1)
- bot_kill_switch_state: 0=safe, 1=armed, 2=triggered
- bot_heartbeat_age: Seconds since last heartbeat
- bot_reconciliation_drift: Last reconciliation drift in USD

Usage:
    from reporting.prometheus_exporter import PrometheusExporter

    metrics = PrometheusExporter(port=8000)
    metrics.start()

    # Update metrics during trading
    metrics.update_equity(520.50)
    metrics.update_drawdown(5.2)
    metrics.update_daily_pnl(2.35)
"""

import logging
from decimal import Decimal
from typing import Optional

from prometheus_client import start_http_server, Gauge, Counter

logger = logging.getLogger(__name__)


class PrometheusExporter:
    """Export bot metrics for Prometheus scraping."""

    def __init__(self, port: int = 8000, bot_id: str = "bot_a"):
        self.port = port
        self.bot_id = bot_id

        # Gauges
        self.equity = Gauge("bot_equity", "Current equity USD", ["bot_id"])
        self.drawdown = Gauge("bot_drawdown_pct", "Current drawdown %", ["bot_id"])
        self.daily_pnl = Gauge("bot_daily_pnl", "Daily P&L USD", ["bot_id"])
        self.win_rate = Gauge("bot_win_rate_pct", "Win rate %", ["bot_id"])
        self.open_positions = Gauge("bot_open_positions", "Open position count", ["bot_id"])
        self.signal_strength = Gauge("bot_signal_strength", "Last signal strength", ["bot_id"])
        self.kill_switch = Gauge("bot_kill_switch_state", "Kill switch state", ["bot_id"])
        self.heartbeat_age = Gauge("bot_heartbeat_age_sec", "Seconds since heartbeat", ["bot_id"])
        self.recon_drift = Gauge("bot_reconciliation_drift_usd", "Reconciliation drift USD", ["bot_id"])

        # Counters
        self.trades_total = Counter("bot_trades_total", "Total trades", ["bot_id", "side", "symbol"])
        self.alerts_total = Counter("bot_alerts_total", "Total alerts sent", ["bot_id", "severity"])

        self._started = False

    def start(self):
        """Start HTTP server for Prometheus scraping."""
        if not self._started:
            start_http_server(self.port)
            self._started = True
            logger.info(f"Prometheus metrics server started on port {self.port}")

    def update_equity(self, equity: float):
        self.equity.labels(bot_id=self.bot_id).set(equity)

    def update_drawdown(self, drawdown_pct: float):
        self.drawdown.labels(bot_id=self.bot_id).set(drawdown_pct)

    def update_daily_pnl(self, pnl: float):
        self.daily_pnl.labels(bot_id=self.bot_id).set(pnl)

    def update_win_rate(self, win_rate_pct: float):
        self.win_rate.labels(bot_id=self.bot_id).set(win_rate_pct)

    def update_open_positions(self, count: int):
        self.open_positions.labels(bot_id=self.bot_id).set(count)

    def update_signal_strength(self, strength: float):
        self.signal_strength.labels(bot_id=self.bot_id).set(strength)

    def update_kill_switch(self, state: int):
        """0=safe, 1=armed, 2=triggered"""
        self.kill_switch.labels(bot_id=self.bot_id).set(state)

    def update_heartbeat_age(self, age_sec: float):
        self.heartbeat_age.labels(bot_id=self.bot_id).set(age_sec)

    def update_reconciliation_drift(self, drift_usd: float):
        self.recon_drift.labels(bot_id=self.bot_id).set(drift_usd)

    def record_trade(self, side: str, symbol: str):
        self.trades_total.labels(bot_id=self.bot_id, side=side, symbol=symbol).inc()

    def record_alert(self, severity: str):
        self.alerts_total.labels(bot_id=self.bot_id, severity=severity).inc()

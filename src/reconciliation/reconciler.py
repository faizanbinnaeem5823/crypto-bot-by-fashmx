"""
Reconciliation module (R3): Compares bot-recorded state vs exchange-reported state.
Runs on a configurable interval, triggers kill switch on excessive drift.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from state.state_manager import StateManager
from exchange.binance_client import BinanceClient
from risk.kill_switch import KillSwitch

logger = logging.getLogger(__name__)


@dataclass
class DriftRecord:
    """Snapshot of a single reconciliation check."""
    timestamp: datetime
    bot_equity: Decimal
    exchange_equity: Decimal
    drift: Decimal
    drift_usd: float
    passed: bool


class Reconciler:
    """
    Periodically reconciles the bot's internal equity state against the
    exchange's reported balances.  Triggers a kill switch if the absolute
    drift exceeds *max_drift_usd*.
    """

    def __init__(
        self,
        state_manager: StateManager,
        exchange_client: BinanceClient,
        kill_switch: KillSwitch,
        interval_sec: int = 60,
        max_drift_usd: float = 1.00,
        history_size: int = 1000,
    ):
        self.state = state_manager
        self.exchange = exchange_client
        self.kill_switch = kill_switch
        self.interval_sec = interval_sec
        self.max_drift_usd = Decimal(str(max_drift_usd))
        self._history: Deque[DriftRecord] = deque(maxlen=history_size)
        self._last_reconciliation: Optional[datetime] = None
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def run_reconciliation(self) -> None:
        """
        Main loop – runs until *self._shutdown_event* is set.
        Sleeps asynchronously so it does not block the event loop.
        """
        logger.info(
            "Reconciler started (interval=%ds, max_drift=$%.2f)",
            self.interval_sec,
            float(self.max_drift_usd),
        )
        while not self._shutdown_event.is_set():
            try:
                await self.check_drift()
            except Exception:
                logger.exception("Reconciliation check failed")
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=self.interval_sec
                )
            except asyncio.TimeoutError:
                pass  # normal – interval expired, run next check
        logger.info("Reconciler stopped gracefully")

    async def check_drift(self) -> DriftRecord:
        """
        Single reconciliation pass.
        1. Fetch bot equity from StateManager.
        2. Fetch exchange-reported equity from BinanceClient.
        3. Compute drift = abs(bot_equity - exchange_equity).
        4. If drift > max_drift_usd → trigger kill switch + critical alert.
        5. Record timestamp and append to history.
        """
        now = datetime.now(timezone.utc)

        # -- 1. Bot-recorded equity --
        try:
            bot_equity = self._get_bot_equity()
        except Exception as exc:
            logger.error("Unable to read bot equity: %s", exc)
            bot_equity = Decimal("0")

        # -- 2. Exchange-reported equity --
        try:
            exchange_equity = await self._get_exchange_equity()
        except Exception as exc:
            logger.error("Unable to read exchange equity: %s", exc)
            exchange_equity = Decimal("0")

        # -- 3. Drift calculation --
        drift = abs(bot_equity - exchange_equity)
        drift_float = float(drift)
        passed = drift <= self.max_drift_usd

        record = DriftRecord(
            timestamp=now,
            bot_equity=bot_equity,
            exchange_equity=exchange_equity,
            drift=drift,
            drift_usd=drift_float,
            passed=passed,
        )
        self._history.append(record)
        self._last_reconciliation = now

        # -- 4. Alerting / kill switch --
        if not passed:
            await self._handle_drift_violation(record)
        else:
            logger.debug(
                "Reconciliation OK | bot=%s exchange=%s drift=$%.4f",
                bot_equity,
                exchange_equity,
                drift_float,
            )

        return record

    def get_last_reconciliation_time(self) -> Optional[datetime]:
        """UTC timestamp of the most recent successful check, or *None*."""
        return self._last_reconciliation

    def get_drift_history(self, limit: int = 100) -> List[Dict]:
        """Return the *limit* most recent drift records as plain dicts."""
        records = list(self._history)[-limit:]
        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "bot_equity": str(r.bot_equity),
                "exchange_equity": str(r.exchange_equity),
                "drift": str(r.drift),
                "drift_usd": r.drift_usd,
                "passed": r.passed,
            }
            for r in records
        ]

    def stop(self) -> None:
        """Signal the reconciler to shut down gracefully."""
        self._shutdown_event.set()

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _get_bot_equity(self) -> Decimal:
        """Read the latest equity row recorded by the bot."""
        rows = self.state.get_equity_curve(limit=1)
        if rows:
            return Decimal(str(rows[0].get("equity", "0")))
        logger.warning("No equity rows found in local DB")
        return Decimal("0")

    async def _get_exchange_equity(self) -> Decimal:
        """Read total equity from the exchange (USDT balance as proxy)."""
        balance = await self.exchange.get_balance("USDT")
        return balance

    async def _handle_drift_violation(self, record: DriftRecord) -> None:
        """Trigger kill switch and emit a critical alert with full breakdown."""
        self.kill_switch.trigger()
        logger.critical(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║           RECONCILIATION DRIFT VIOLATION                     ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  Bot equity:       %-42s║\n"
            "║  Exchange equity:  %-42s║\n"
            "║  Drift:            $%-41.4f║\n"
            "║  Max allowed:      $%-41.2f║\n"
            "║  Timestamp (UTC):  %-42s║\n"
            "╚══════════════════════════════════════════════════════════════╝",
            record.bot_equity,
            record.exchange_equity,
            record.drift_usd,
            float(self.max_drift_usd),
            record.timestamp.isoformat(),
        )

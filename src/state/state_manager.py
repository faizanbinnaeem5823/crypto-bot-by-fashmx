"""
State Manager – DuckDB-backed persistent state for the trading bot.

Includes:
  * R6 – DB resilience (retry logic, healthcheck, reconnection)
  * R8 – Daily / weekly / monthly P&L tracking with UTC midnight resets
"""

import logging
import time
import duckdb
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class StateManagerError(Exception):
    """Fatal error after exhausting all DB retries."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class Trade:
    id: int
    bot_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    timestamp: datetime
    pnl: Optional[Decimal]
    status: str


# --------------------------------------------------------------------------- #
# StateManager
# --------------------------------------------------------------------------- #

class StateManager:
    """
    Manages all DuckDB interactions with automatic retry logic (R6) and
    daily P&L bookkeeping (R8).
    """

    def __init__(
        self,
        db_path: str,
        bot_id: str,
        max_retries: int = 3,
        retry_delay: float = 0.1,
    ):
        self.db_path = db_path
        self.bot_id = bot_id
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.last_error: Optional[str] = None

        self.conn = duckdb.connect(db_path)
        self._init_tables()
        self._ensure_pnl_row()

    # ------------------------------------------------------------------ #
    # R6 – Retry & resilience primitives
    # ------------------------------------------------------------------ #

    def _execute_with_retry(self, sql: str, params=None):
        """
        Execute *sql* with exponential-backoff retry.
        Reconnects automatically on transient DuckDB failures.
        """
        params = params or []
        for attempt in range(self.max_retries):
            try:
                return self.conn.execute(sql, params)
            except (duckdb.ConnectionException, duckdb.IOException) as exc:
                self.last_error = str(exc)
                logger.warning(
                    "DB error (attempt %d/%d): %s", attempt + 1, self.max_retries, exc
                )
                if attempt < self.max_retries - 1:
                    sleep_time = self.retry_delay * (2 ** attempt)
                    time.sleep(sleep_time)
                    self._reconnect()
                else:
                    logger.critical(
                        "DB operation failed after %d retries: %s", self.max_retries, exc
                    )
                    raise StateManagerError(f"Database failure: {exc}") from exc
            except Exception as exc:
                self.last_error = str(exc)
                logger.error("Unexpected DB error: %s", exc)
                raise StateManagerError(f"Unexpected DB error: {exc}") from exc

    def _reconnect(self) -> None:
        """Close and reopen the DuckDB connection."""
        logger.info("Reconnecting to DuckDB at %s", self.db_path)
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = duckdb.connect(self.db_path)

    def healthcheck(self) -> bool:
        """Return *True* if the database is reachable."""
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("Healthcheck failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Schema initialisation
    # ------------------------------------------------------------------ #

    def _init_tables(self) -> None:
        """Create all tables if they do not yet exist."""
        # -- trades (existing) --
        self._execute_with_retry("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY,
                bot_id      VARCHAR,
                symbol      VARCHAR,
                side        VARCHAR,
                quantity    DECIMAL,
                price       DECIMAL,
                timestamp   TIMESTAMP,
                pnl         DECIMAL,
                status      VARCHAR
            )
        """)

        # -- equity snapshots (existing) --
        self._execute_with_retry("""
            CREATE TABLE IF NOT EXISTS equity (
                timestamp   TIMESTAMP,
                bot_id      VARCHAR,
                equity      DECIMAL,
                cash        DECIMAL
            )
        """)

        # -- heartbeat (existing) --
        self._execute_with_retry("""
            CREATE TABLE IF NOT EXISTS heartbeat (
                bot_id      VARCHAR PRIMARY KEY,
                last_beat   TIMESTAMP
            )
        """)

        # -- daily P&L tracking (R8) --
        self._execute_with_retry("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                bot_id      VARCHAR PRIMARY KEY,
                trading_day DATE,
                daily_pnl   DECIMAL DEFAULT 0,
                weekly_pnl  DECIMAL DEFAULT 0,
                monthly_pnl DECIMAL DEFAULT 0,
                last_reset  TIMESTAMP
            )
        """)

        # -- daily reset log (R8) --
        self._execute_with_retry("""
            CREATE TABLE IF NOT EXISTS daily_pnl_reset (
                bot_id      VARCHAR PRIMARY KEY,
                last_reset  TIMESTAMP
            )
        """)

    def _ensure_pnl_row(self) -> None:
        """Insert a default P&L row for this bot if absent."""
        today = date.today()
        self._execute_with_retry("""
            INSERT OR IGNORE INTO daily_pnl (bot_id, trading_day, daily_pnl, weekly_pnl, monthly_pnl, last_reset)
            VALUES (?, ?, 0, 0, 0, ?)
        """, [self.bot_id, today, datetime.now(timezone.utc)])

    # ------------------------------------------------------------------ #
    # Trades (existing functionality – now with retries)
    # ------------------------------------------------------------------ #

    def record_trade(self, trade: Trade) -> None:
        self._execute_with_retry("""
            INSERT INTO trades (id, bot_id, symbol, side, quantity, price, timestamp, pnl, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            trade.id, trade.bot_id, trade.symbol, trade.side,
            trade.quantity, trade.price, trade.timestamp, trade.pnl, trade.status,
        ])

        # Accumulate realised P&L into today's counter (R8)
        if trade.pnl is not None:
            self._accumulate_pnl(trade.pnl)

    def get_recent_trades(self, limit: int = 100) -> List[Dict]:
        result = self._execute_with_retry(
            "SELECT * FROM trades WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?",
            [self.bot_id, limit],
        ).fetchall()
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in result]

    # ------------------------------------------------------------------ #
    # Equity (existing functionality – now with retries)
    # ------------------------------------------------------------------ #

    def update_equity(self, equity: Decimal, cash: Decimal) -> None:
        self._execute_with_retry("""
            INSERT INTO equity (timestamp, bot_id, equity, cash) VALUES (?, ?, ?, ?)
        """, [datetime.now(timezone.utc), self.bot_id, equity, cash])

    def get_equity_curve(self, limit: int = 1000) -> List[Dict]:
        result = self._execute_with_retry(
            "SELECT * FROM equity WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?",
            [self.bot_id, limit],
        ).fetchall()
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in result]

    # ------------------------------------------------------------------ #
    # Heartbeat (existing functionality – now with retries)
    # ------------------------------------------------------------------ #

    def heartbeat(self) -> None:
        self._execute_with_retry("""
            INSERT OR REPLACE INTO heartbeat (bot_id, last_beat) VALUES (?, ?)
        """, [self.bot_id, datetime.now(timezone.utc)])

    def get_heartbeat_age(self) -> float:
        now_utc = datetime.now(timezone.utc)
        result = self._execute_with_retry(
            "SELECT EXTRACT(EPOCH FROM (?::TIMESTAMP - last_beat)) FROM heartbeat WHERE bot_id = ?",
            [now_utc, self.bot_id],
        ).fetchone()
        return result[0] if result else float("inf")

    # ------------------------------------------------------------------ #
    # R8 – Daily / weekly / monthly P&L
    # ------------------------------------------------------------------ #

    def _accumulate_pnl(self, pnl: Decimal) -> None:
        """Add *pnl* to the running daily / weekly / monthly counters."""
        self._execute_with_retry("""
            UPDATE daily_pnl
               SET daily_pnl  = daily_pnl + ?,
                   weekly_pnl = weekly_pnl + ?,
                   monthly_pnl = monthly_pnl + ?
             WHERE bot_id = ?
        """, [pnl, pnl, pnl, self.bot_id])

    def get_daily_pnl(self) -> Decimal:
        """Return today's realised P&L (since last midnight UTC)."""
        row = self._execute_with_retry(
            "SELECT daily_pnl FROM daily_pnl WHERE bot_id = ?",
            [self.bot_id],
        ).fetchone()
        return row[0] if row else Decimal("0")

    def get_weekly_pnl(self) -> Decimal:
        """Return this week's realised P&L."""
        row = self._execute_with_retry(
            "SELECT weekly_pnl FROM daily_pnl WHERE bot_id = ?",
            [self.bot_id],
        ).fetchone()
        return row[0] if row else Decimal("0")

    def get_monthly_pnl(self) -> Decimal:
        """Return this month's realised P&L."""
        row = self._execute_with_retry(
            "SELECT monthly_pnl FROM daily_pnl WHERE bot_id = ?",
            [self.bot_id],
        ).fetchone()
        return row[0] if row else Decimal("0")

    def get_last_reset_time(self) -> Optional[datetime]:
        """UTC timestamp of the last P&L reset, or *None*."""
        row = self._execute_with_retry(
            "SELECT last_reset FROM daily_pnl WHERE bot_id = ?",
            [self.bot_id],
        ).fetchone()
        if row and row[0]:
            # DuckDB returns naive datetimes – treat as UTC
            ts = row[0]
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts
        return None

    def should_reset_pnl(self) -> bool:
        """
        Return *True* if we have crossed a UTC midnight boundary since the
        last reset (or if no reset has ever been performed).
        """
        now_utc = datetime.now(timezone.utc)
        last_reset = self.get_last_reset_time()
        if last_reset is None:
            return True
        # Reset when the UTC date has changed
        return now_utc.date() != last_reset.date()

    def reset_daily_pnl(self) -> None:
        """
        Reset the daily P&L counter at midnight UTC.
        Weekly and monthly counters are reset when their respective
        boundaries are crossed.
        """
        now_utc = datetime.now(timezone.utc)
        today = now_utc.date()

        last_reset = self.get_last_reset_time()
        reset_weekly = False
        reset_monthly = False

        if last_reset is not None:
            # ISO calendar: same week if year and week number match
            if today.isocalendar()[:2] != last_reset.date().isocalendar()[:2]:
                reset_weekly = True
            if today.month != last_reset.date().month or today.year != last_reset.date().year:
                reset_monthly = True
        else:
            # First ever run – reset everything
            reset_weekly = True
            reset_monthly = True

        self._execute_with_retry("""
            INSERT INTO daily_pnl_reset (bot_id, last_reset)
            VALUES (?, ?)
            ON CONFLICT (bot_id) DO UPDATE SET last_reset = excluded.last_reset
        """, [self.bot_id, now_utc])

        self._execute_with_retry("""
            INSERT INTO daily_pnl (bot_id, trading_day, daily_pnl, weekly_pnl, monthly_pnl, last_reset)
            VALUES (?, ?, 0, 0, 0, ?)
            ON CONFLICT (bot_id) DO UPDATE
               SET trading_day = excluded.trading_day,
                   daily_pnl   = 0,
                   weekly_pnl  = CASE WHEN ? THEN 0 ELSE daily_pnl.weekly_pnl END,
                   monthly_pnl = CASE WHEN ? THEN 0 ELSE daily_pnl.monthly_pnl END,
                   last_reset  = excluded.last_reset
        """, [
            self.bot_id, today, now_utc,
            reset_weekly, reset_monthly,
        ])

        logger.info(
            "P&L reset for %s (daily=0, weekly_reset=%s, monthly_reset=%s)",
            self.bot_id, reset_weekly, reset_monthly,
        )

    def maybe_reset_pnl(self) -> bool:
        """
        Convenience wrapper: resets P&L only if a day boundary has been
        crossed.  Returns *True* when a reset actually occurred.
        """
        if self.should_reset_pnl():
            self.reset_daily_pnl()
            return True
        return False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

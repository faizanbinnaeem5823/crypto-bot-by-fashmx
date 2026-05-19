"""Circuit breakers with persistent peak equity and multi-timeframe limits.

Tracks peak equity persistently (via optional Redis) so drawdown is never
reset by bot restarts. Enforces daily, weekly, and monthly loss limits with
UTC timestamps on all log entries.

Paranoid rule: if peak equity cannot be loaded from storage, the bot defaults
to the first equity reading — it NEVER starts with an optimistic assumption.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CircuitBreakers:
    """Multi-timeframe circuit breakers with persistent peak equity tracking.

    All monetary values are Decimal. All timestamps are UTC.
    """

    # Redis key for persistent peak equity storage
    REDIS_KEY_PEAK_EQUITY = "cryptobot:risk:peak_equity"
    REDIS_KEY_PEAK_DATE = "cryptobot:risk:peak_equity_date"

    def __init__(self, config: dict, redis_client: Any = None):
        self.config = config
        self._redis = redis_client

        # In-memory cache of peak equity (may be synced from Redis)
        self._peak_equity: Optional[Decimal] = None

        # Load persistent peak equity if available
        self._load_peak_equity()

    # ------------------------------------------------------------------
    # Persistent peak equity
    # ------------------------------------------------------------------

    def record_equity(self, equity: Decimal) -> Decimal:
        """Record equity snapshot and update peak if new high.

        Persists the peak to Redis (if available) so it survives restarts.
        Returns the current peak equity after recording.
        """
        if self._peak_equity is None:
            # First equity reading — initialize peak conservatively
            self._peak_equity = equity
            logger.info(
                f"[{self._utc_now_str()}] Peak equity initialised: "
                f"{self._peak_equity}"
            )
            self._persist_peak()
            return self._peak_equity

        if equity > self._peak_equity:
            self._peak_equity = equity
            logger.info(
                f"[{self._utc_now_str()}] New peak equity: {self._peak_equity}"
            )
            self._persist_peak()
        return self._peak_equity

    def get_peak_equity(self) -> Optional[Decimal]:
        """Return the stored peak equity (loads from Redis if not cached)."""
        if self._peak_equity is None and self._redis is not None:
            self._load_peak_equity()
        return self._peak_equity

    def _persist_peak(self):
        """Write peak equity to Redis for persistence across restarts."""
        if self._redis is not None and self._peak_equity is not None:
            try:
                self._redis.set(
                    self.REDIS_KEY_PEAK_EQUITY, str(self._peak_equity)
                )
                self._redis.set(
                    self.REDIS_KEY_PEAK_DATE,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                logger.error(
                    f"[{self._utc_now_str()}] "
                    f"Failed to persist peak equity to Redis: {exc}"
                )

    def _load_peak_equity(self):
        """Load peak equity from Redis into local cache."""
        if self._redis is not None:
            try:
                stored = self._redis.get(self.REDIS_KEY_PEAK_EQUITY)
                if stored is not None:
                    stored_str = (
                        stored.decode("utf-8")
                        if isinstance(stored, bytes)
                        else str(stored)
                    )
                    self._peak_equity = Decimal(stored_str)
                    ts = self._redis.get(self.REDIS_KEY_PEAK_DATE)
                    logger.info(
                        f"[{self._utc_now_str()}] "
                        f"Loaded persistent peak equity: {self._peak_equity} "
                        f"(recorded_at={ts})"
                    )
            except Exception as exc:
                logger.error(
                    f"[{self._utc_now_str()}] "
                    f"Failed to load peak equity from Redis: {exc}. "
                    f"Will re-initialize from first equity reading."
                )

    # ------------------------------------------------------------------
    # Drawdown check (uses persistent peak)
    # ------------------------------------------------------------------

    def check_max_drawdown(self, peak: Decimal, current: Decimal) -> bool:
        """Return True if drawdown exceeds kill threshold."""
        if peak <= 0:
            logger.warning(
                f"[{self._utc_now_str()}] "
                f"Invalid peak equity ({peak}) — skipping drawdown check"
            )
            return False
        drawdown_pct = (peak - current) / peak * Decimal("100")
        threshold = Decimal(str(self.config.get("max_drawdown_kill_pct", "0")))
        if drawdown_pct >= threshold:
            logger.critical(
                f"[{self._utc_now_str()}] "
                f"MAX DRAWDOWN KILL: {drawdown_pct:.4f}% "
                f"(peak={peak}, current={current}, threshold={threshold}%)"
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Daily / weekly / monthly loss limits
    # ------------------------------------------------------------------

    def check_daily_limit(self, portfolio_value: Decimal, daily_pnl: Decimal) -> bool:
        """Return True if daily loss is within acceptable bounds."""
        limit = portfolio_value * Decimal(str(self.config.get("daily_cap_pct", "0"))) / Decimal("100")
        if daily_pnl < 0 and abs(daily_pnl) >= limit:
            logger.warning(
                f"[{self._utc_now_str()}] "
                f"Daily limit breached: PnL={daily_pnl}, limit={limit}"
            )
            return False
        return True

    def check_weekly_limit(self, portfolio_value: Decimal, weekly_pnl: Decimal) -> bool:
        """Return True if weekly cumulative loss is within acceptable bounds."""
        cap_key = "weekly_cap_pct"
        if cap_key not in self.config:
            # Back-compat: if not configured, default to 2x daily cap
            weekly_pct = Decimal(str(self.config.get("daily_cap_pct", "0"))) * Decimal("2")
        else:
            weekly_pct = Decimal(str(self.config[cap_key]))

        limit = portfolio_value * weekly_pct / Decimal("100")
        if weekly_pnl < 0 and abs(weekly_pnl) >= limit:
            logger.warning(
                f"[{self._utc_now_str()}] "
                f"Weekly limit breached: PnL={weekly_pnl}, limit={limit}"
            )
            return False
        return True

    def check_monthly_limit(self, portfolio_value: Decimal, monthly_pnl: Decimal) -> bool:
        """Return True if monthly cumulative loss is within acceptable bounds."""
        cap_key = "monthly_cap_pct"
        if cap_key not in self.config:
            # Back-compat: if not configured, default to 4x daily cap
            monthly_pct = Decimal(str(self.config.get("daily_cap_pct", "0"))) * Decimal("4")
        else:
            monthly_pct = Decimal(str(self.config[cap_key]))

        limit = portfolio_value * monthly_pct / Decimal("100")
        if monthly_pnl < 0 and abs(monthly_pnl) >= limit:
            logger.warning(
                f"[{self._utc_now_str()}] "
                f"Monthly limit breached: PnL={monthly_pnl}, limit={limit}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Aggregate checks
    # ------------------------------------------------------------------

    def check_all_limits(
        self,
        portfolio_value: Decimal,
        daily_pnl: Decimal,
        weekly_pnl: Decimal,
        monthly_pnl: Decimal,
    ) -> tuple[bool, str]:
        """Check ALL limits in sequence. Returns (allowed, reason).

        Checks are ordered from most to least severe:
        1. Max drawdown (most severe — triggers kill)
        2. Daily limit
        3. Weekly limit
        4. Monthly limit
        """
        # Drawdown check using persistent peak equity
        peak = self.get_peak_equity()
        if peak is not None:
            if self.check_max_drawdown(peak, portfolio_value):
                return False, "max_drawdown_kill"

        if not self.check_daily_limit(portfolio_value, daily_pnl):
            return False, "daily_limit"

        if not self.check_weekly_limit(portfolio_value, weekly_pnl):
            return False, "weekly_limit"

        if not self.check_monthly_limit(portfolio_value, monthly_pnl):
            return False, "monthly_limit"

        return True, "all_limits_passed"

    def should_trigger_kill(
        self,
        portfolio_value: Decimal,
        daily_pnl: Decimal,
        weekly_pnl: Decimal = Decimal("0"),
        monthly_pnl: Decimal = Decimal("0"),
    ) -> bool:
        """Convenience method: returns True if ANY limit is breached.

        This is a single-call check suitable for the main trading loop.
        """
        allowed, reason = self.check_all_limits(
            portfolio_value, daily_pnl, weekly_pnl, monthly_pnl
        )
        if not allowed:
            logger.critical(
                f"[{self._utc_now_str()}] "
                f"Circuit breaker KILL triggered: {reason}"
            )
        return not allowed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utc_now_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

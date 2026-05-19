"""Risk engine: paranoid pre-flight checks before ANY order reaches the exchange.

Every order submission MUST pass through this engine. The flow:

    1. check_and_enforce()  -> raises KillSwitchError if triggered
    2. check_all_limits()   -> daily/weekly/monthly drawdown checks
    3. calculate_position_size() -> conservative sizing

All monetary values use Decimal. All timestamps use UTC.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .circuit_breakers import CircuitBreakers
from .kill_switch import KillSwitch, KillSwitchError

logger = logging.getLogger(__name__)


class RiskEngine:
    """Central risk enforcement engine.

    Parameters
    ----------
    config : dict
        Risk parameters: per_trade_risk_pct, daily_cap_pct, weekly_cap_pct,
        monthly_cap_pct, max_drawdown_kill_pct, max_position_pct, etc.
    bot_id : str
        Unique identifier for this bot instance (logging / Redis namespacing).
    redis_client : Any, optional
        Redis connection for shared kill-switch state and persistent peak equity.
    """

    REDIS_KEY_DAILY_PNL_RESET = "cryptobot:risk:daily_pnl_reset_date"

    def __init__(
        self,
        config: dict,
        bot_id: str,
        redis_client: Any = None,
    ):
        self.config = config
        self.bot_id = bot_id
        self._redis = redis_client

        # Pass redis_client to KillSwitch for cross-bot coordination
        self.kill_switch = KillSwitch(redis_client=redis_client)

        # Pass redis_client to CircuitBreakers for persistent peak equity
        self.circuit_breakers = CircuitBreakers(config, redis_client=redis_client)

        # Track last daily PnL reset date (persisted in Redis if available)
        self._last_pnl_reset_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self, portfolio_value: Decimal, signal_strength: float
    ) -> Decimal:
        """Conservative position sizing: signal must be strong AND capital available."""
        # Minimum signal threshold — weak signals get zero size
        if signal_strength < 0.3:
            logger.debug(
                f"[{self._utc_now_str()}] Signal too weak ({signal_strength:.3f}) — "
                f"zero position"
            )
            return Decimal("0")

        risk_per_trade = Decimal(str(self.config.get("per_trade_risk_pct", "0"))) / Decimal("100")
        size = portfolio_value * risk_per_trade * Decimal(str(signal_strength))

        # Hard cap: position cannot exceed max_position_pct of portfolio
        max_position_pct = Decimal(str(self.config.get("max_position_pct", "100"))) / Decimal("100")
        max_position = portfolio_value * max_position_pct
        if size > max_position:
            logger.warning(
                f"[{self._utc_now_str()}] Position size capped: "
                f"{size} -> {max_position} (max_position_pct limit)"
            )
            size = max_position

        return size.quantize(Decimal("0.00001"))

    # ------------------------------------------------------------------
    # Kill-switch enforcement — CALL THIS BEFORE EVERY ORDER
    # ------------------------------------------------------------------

    def check_and_enforce(self):
        """Pre-order gate: raises KillSwitchError if kill switch is triggered.

        Every order submission MUST call this method first. No exceptions.
        """
        if self.kill_switch.is_triggered():
            raise KillSwitchError(
                f"[{self._utc_now_str()}] bot_id={self.bot_id}: "
                f"ORDER BLOCKED — Kill switch is TRIGGERED."
            )

    # ------------------------------------------------------------------
    # Drawdown enforcement — triggers kill if max drawdown breached
    # ------------------------------------------------------------------

    def enforce_drawdown_check(
        self, peak_equity: Decimal, current_equity: Decimal
    ) -> tuple[bool, str]:
        """Check drawdown against peak equity and trigger kill if breached.

        Updates persistent peak equity via circuit_breakers. Returns
        (allowed, reason). If drawdown kills, the kill switch is triggered
        automatically.
        """
        # Record equity updates peak persistently
        recorded_peak = self.circuit_breakers.record_equity(peak_equity)

        if recorded_peak <= 0:
            logger.error(
                f"[{self._utc_now_str()}] Invalid peak equity: {recorded_peak}"
            )
            return False, "invalid_peak_equity"

        if self.circuit_breakers.check_max_drawdown(recorded_peak, current_equity):
            drawdown_pct = (recorded_peak - current_equity) / recorded_peak * Decimal("100")
            self.kill_switch.trigger()
            logger.critical(
                f"[{self._utc_now_str()}] bot_id={self.bot_id}: "
                f"KILL SWITCH AUTO-TRIGGERED by max drawdown: "
                f"{drawdown_pct:.4f}% (peak={recorded_peak}, current={current_equity})"
            )
            return False, f"max_drawdown_kill_{drawdown_pct:.4f}%"

        return True, "drawdown_ok"

    # ------------------------------------------------------------------
    # Full trade permission check
    # ------------------------------------------------------------------

    def check_trade_allowed(
        self,
        portfolio_value: Decimal,
        daily_pnl: Decimal,
        weekly_pnl: Decimal = Decimal("0"),
        monthly_pnl: Decimal = Decimal("0"),
    ) -> tuple[bool, str]:
        """Full pre-flight check: kill switch + all circuit breakers.

        Returns (allowed, reason). If any check fails, the reason string
        identifies which limit was breached.
        """
        # 1. Kill switch (highest priority)
        self.check_and_enforce()  # raises KillSwitchError if triggered

        # 2. All circuit breaker limits
        allowed, reason = self.circuit_breakers.check_all_limits(
            portfolio_value, daily_pnl, weekly_pnl, monthly_pnl
        )
        if not allowed:
            logger.warning(
                f"[{self._utc_now_str()}] bot_id={self.bot_id}: "
                f"Trade blocked — {reason}"
            )
            return False, reason

        return True, "OK"

    # ------------------------------------------------------------------
    # Daily PnL reset tracking (for rollover at UTC midnight)
    # ------------------------------------------------------------------

    def reset_daily_pnl_if_new_day(self, last_reset: Optional[datetime] = None) -> bool:
        """Check if daily PnL should be reset (new UTC day).

        Compares the stored last-reset date (from Redis if available) against
        current UTC date. Returns True if a reset was performed.

        Parameters
        ----------
        last_reset : datetime, optional
            The last known reset datetime. If None, loads from Redis.
        """
        now = datetime.now(timezone.utc)
        today = now.date()

        if last_reset is not None:
            self._last_pnl_reset_date = last_reset.date()
        elif self._redis is not None:
            try:
                stored = self._redis.get(self.REDIS_KEY_DAILY_PNL_RESET)
                if stored is not None:
                    stored_str = (
                        stored.decode("utf-8")
                        if isinstance(stored, bytes)
                        else str(stored)
                    )
                    self._last_pnl_reset_date = date.fromisoformat(stored_str)
            except Exception as exc:
                logger.error(
                    f"[{self._utc_now_str()}] Failed to load daily PnL reset date: {exc}"
                )

        if self._last_pnl_reset_date is None or self._last_pnl_reset_date < today:
            logger.info(
                f"[{self._utc_now_str()}] New UTC day detected ({today}). "
                f"Resetting daily PnL tracking."
            )
            self._last_pnl_reset_date = today

            # Persist reset date to Redis
            if self._redis is not None:
                try:
                    self._redis.set(
                        self.REDIS_KEY_DAILY_PNL_RESET, today.isoformat()
                    )
                except Exception as exc:
                    logger.error(
                        f"[{self._utc_now_str()}] Failed to persist daily reset date: {exc}"
                    )
            return True

        return False

    # ------------------------------------------------------------------
    # Convenience: full order validation pipeline
    # ------------------------------------------------------------------

    def validate_order(
        self,
        portfolio_value: Decimal,
        current_equity: Decimal,
        daily_pnl: Decimal,
        weekly_pnl: Decimal,
        monthly_pnl: Decimal,
        signal_strength: float,
    ) -> tuple[bool, str, Decimal]:
        """Full validation pipeline for a single order.

        Returns (allowed, reason, position_size). If not allowed, position_size
        is always Decimal('0').

        This is the ONE method the trading bot should call before submitting
        any order to the exchange.
        """
        try:
            # 1. Kill switch + circuit breakers
            allowed, reason = self.check_trade_allowed(
                portfolio_value, daily_pnl, weekly_pnl, monthly_pnl
            )
            if not allowed:
                return False, reason, Decimal("0")

            # 2. Drawdown check (auto-triggers kill if breached)
            dd_ok, dd_reason = self.enforce_drawdown_check(
                self.circuit_breakers.get_peak_equity() or portfolio_value,
                current_equity,
            )
            if not dd_ok:
                return False, dd_reason, Decimal("0")

            # 3. Position sizing
            size = self.calculate_position_size(portfolio_value, signal_strength)
            if size <= 0:
                return False, "zero_position_size", Decimal("0")

            return True, "OK", size

        except KillSwitchError as exc:
            logger.critical(f"[{self._utc_now_str()}] Order rejected by kill switch: {exc}")
            return False, "kill_switch_triggered", Decimal("0")
        except Exception as exc:
            # Paranoid: ANY exception blocks the order
            logger.critical(
                f"[{self._utc_now_str()}] Risk validation FAILED with exception: {exc}. "
                f"Order BLOCKED."
            )
            return False, f"risk_validation_error: {exc}", Decimal("0")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utc_now_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

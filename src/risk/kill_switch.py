"""Kill switch with cross-bot Redis propagation for distributed halt coordination.

Paranoid safety: on trigger, publishes to a shared Redis channel so ALL bots
in the fleet halt within seconds. On startup, reads Redis to detect an existing
kill state so restarts don't accidentally reset a triggered kill.

Usage:
    ks = KillSwitch(redis_client=redis)
    await ks.check_and_propagate()  # poll loop
    ks.check_and_raise()            # pre-flight gate
"""

import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

# NEW: Exception for kill switch rejection
class KillSwitchError(Exception):
    """Raised when kill switch is triggered and trading is blocked."""
    pass


class KillState(Enum):
    SAFE = "safe"
    ARMED = "armed"
    TRIGGERED = "triggered"


class KillSwitch:
    """Distributed kill switch backed by optional Redis shared state.

    Local state is always the source of truth for *this* process, but Redis
    provides cross-bot awareness. If Redis is unavailable, the kill switch
    degrades gracefully to local-only operation (safer than crashing).
    """

    # Redis keys used for coordination
    REDIS_KEY_STATE = "cryptobot:kill:state"
    REDIS_KEY_TIMESTAMP = "cryptobot:kill:timestamp"
    REDIS_CHANNEL = "cryptobot:kill"

    def __init__(self, redis_client: Any = None):
        self._state = KillState.SAFE
        self._redis = redis_client
        self._last_poll_at: Optional[datetime] = None

        # On startup: check Redis for existing kill state so restart
        # doesn't accidentally reset a triggered kill.
        if self._redis is not None:
            try:
                existing = self._redis.get(self.REDIS_KEY_STATE)
                if existing is not None:
                    existing_str = (
                        existing.decode("utf-8")
                        if isinstance(existing, bytes)
                        else str(existing)
                    )
                    if existing_str == KillState.TRIGGERED.value:
                        self._state = KillState.TRIGGERED
                        ts = self._redis.get(self.REDIS_KEY_TIMESTAMP)
                        logger.critical(
                            "Kill switch loaded TRIGGERED state from Redis "
                            f"(triggered_at={ts}) — ALL BOTS HALTED"
                        )
                    elif existing_str == KillState.ARMED.value:
                        self._state = KillState.ARMED
                        logger.warning(
                            "Kill switch loaded ARMED state from Redis"
                        )
            except Exception as exc:
                # Degrade gracefully: log error but don't crash on Redis failure.
                logger.error(
                    f"KillSwitch Redis init check failed: {exc}. "
                    "Continuing with local-only mode."
                )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def arm(self):
        self._state = KillState.ARMED
        logger.warning("Kill switch ARMED")
        # Persist to Redis for cross-bot visibility
        self._persist_state_to_redis()

    def trigger(self):
        """Trigger local kill AND publish to Redis so ALL bots halt."""
        self._state = KillState.TRIGGERED
        now = datetime.now(timezone.utc).isoformat()
        logger.critical("KILL SWITCH TRIGGERED - ALL BOTS HALTED")

        # Publish kill event to Redis channel for real-time cross-bot propagation
        if self._redis is not None:
            try:
                self._redis.set(self.REDIS_KEY_STATE, KillState.TRIGGERED.value)
                self._redis.set(self.REDIS_KEY_TIMESTAMP, now)
                self._redis.publish(
                    self.REDIS_CHANNEL,
                    f"TRIGGERED|{now}",
                )
                logger.critical(
                    f"Published kill event to Redis channel '{self.REDIS_CHANNEL}'"
                )
            except Exception as exc:
                # Still triggered locally; Redis failure must not swallow the kill.
                logger.error(
                    f"Failed to publish kill event to Redis: {exc}. "
                    "Local kill is ACTIVE but other bots may not see it."
                )
        else:
            logger.warning(
                "Kill switch triggered but no Redis client — "
                "cross-bot propagation disabled"
            )

    def reset(self):
        """Reset kill switch to SAFE — requires manual intervention."""
        self._state = KillState.SAFE
        logger.info("Kill switch reset to SAFE")
        if self._redis is not None:
            try:
                self._redis.delete(self.REDIS_KEY_STATE)
                self._redis.delete(self.REDIS_KEY_TIMESTAMP)
                self._redis.publish(
                    self.REDIS_CHANNEL,
                    f"RESET|{datetime.now(timezone.utc).isoformat()}",
                )
                logger.info("Published kill RESET to Redis")
            except Exception as exc:
                logger.error(f"Failed to publish kill reset to Redis: {exc}")

    # ------------------------------------------------------------------
    # Queries — local + Redis (if available)
    # ------------------------------------------------------------------

    def is_armed(self) -> bool:
        return self._state == KillState.ARMED

    def is_triggered(self) -> bool:
        """Check BOTH local state AND Redis (if available) for triggered state."""
        if self._state == KillState.TRIGGERED:
            return True
        # Secondary check: query Redis for cross-bot kill state
        if self._redis is not None:
            try:
                remote_state = self._redis.get(self.REDIS_KEY_STATE)
                if remote_state is not None:
                    remote_str = (
                        remote_state.decode("utf-8")
                        if isinstance(remote_state, bytes)
                        else str(remote_state)
                    )
                    if remote_str == KillState.TRIGGERED.value:
                        # Sync local state to match Redis — another bot triggered it
                        if self._state != KillState.TRIGGERED:
                            self._state = KillState.TRIGGERED
                            logger.critical(
                                "Kill switch synced to TRIGGERED from Redis "
                                "(another bot triggered it)"
                            )
                        return True
            except Exception as exc:
                logger.error(
                    f"Redis check in is_triggered() failed: {exc}. "
                    "Falling back to local state only."
                )
        return False

    # ------------------------------------------------------------------
    # Order-path enforcement: raises KillSwitchError if triggered
    # ------------------------------------------------------------------

    def check_and_raise(self):
        """Pre-flight gate for every order. Raises KillSwitchError if triggered.

        Call this BEFORE any order submission to the exchange.
        """
        if self.is_triggered():
            raise KillSwitchError(
                "ORDER REJECTED: Kill switch is TRIGGERED. "
                "No trading until manually reset."
            )

    # ------------------------------------------------------------------
    # Async propagation: poll Redis and sync local state
    # ------------------------------------------------------------------

    async def check_and_propagate(self, poll_interval_sec: float = 5.0):
        """Background task: poll Redis for remote kill state and sync locally.

        Run this as an asyncio task. It propagates kill events from other bots
        into this bot's local state. The trigger() method also publishes,
        so this polling catches kills from other bots that may have missed
        the pub/sub message.
        """
        logger.info(
            f"KillSwitch check_and_propagate started "
            f"(poll_interval={poll_interval_sec}s)"
        )
        while True:
            try:
                self.is_triggered()  # side-effect: syncs Redis state to local
                self._last_poll_at = datetime.now(timezone.utc)
            except Exception as exc:
                logger.error(
                    f"Error in check_and_propagate poll: {exc}"
                )
            await asyncio.sleep(poll_interval_sec)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _persist_state_to_redis(self):
        """Write current local state to Redis for cross-bot visibility."""
        if self._redis is not None:
            try:
                self._redis.set(self.REDIS_KEY_STATE, self._state.value)
                self._redis.set(
                    self.REDIS_KEY_TIMESTAMP,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                logger.error(
                    f"Failed to persist kill state to Redis: {exc}"
                )

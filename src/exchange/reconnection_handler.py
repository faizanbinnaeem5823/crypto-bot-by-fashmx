"""Reconnection handler with configurable backoff strategies.

Provides ExponentialBackoff for calculating retry delays and ReconnectionHandler
for managing connection lifecycle with health checks and failure tracking.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExponentialBackoff:
    """Exponential backoff calculator with jitter.

    Delays: base, base*multiplier, base*multiplier^2, ... capped at max_delay.
    Adds random jitter to prevent thundering herd on reconnect.

    Args:
        base: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay cap in seconds (default: 60.0)
        multiplier: Exponential growth factor (default: 2.0)
        jitter: Randomization factor 0..1 (default: 0.1 = 10%)
    """

    base: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.1
    attempt: int = field(default=0, repr=False)

    def next_delay(self) -> float:
        """Calculate the next retry delay with jitter.

        Returns:
            Delay in seconds before the next reconnection attempt.
        """
        delay = min(self.base * (self.multiplier**self.attempt), self.max_delay)
        # Add jitter: +/- jitter% of delay
        jitter_amount = delay * self.jitter
        delay += random.uniform(-jitter_amount, jitter_amount)
        delay = max(delay, 0.1)  # Minimum 100ms
        self.attempt += 1
        logger.debug(
            "Backoff delay=%.2fs attempt=%d max=%.1fs",
            delay,
            self.attempt,
            self.max_delay,
        )
        return delay

    def reset(self) -> None:
        """Reset attempt counter on successful connection."""
        if self.attempt > 0:
            logger.debug("Backoff reset after %d attempts", self.attempt)
        self.attempt = 0

    @property
    def is_at_max(self) -> bool:
        """True if the next delay will hit the max_delay cap."""
        return self.base * (self.multiplier**self.attempt) >= self.max_delay


class ReconnectionHandler:
    """Manages WebSocket reconnection lifecycle with health checks.

    Tracks consecutive failures, decides whether to keep retrying,
    and coordinates with a Backoff strategy for delay calculation.

    Args:
        backoff: Backoff strategy instance (default: ExponentialBackoff)
        max_consecutive_failures: Absolute retry limit (default: 10)
    """

    def __init__(
        self,
        backoff: Optional[ExponentialBackoff] = None,
        max_consecutive_failures: int = 10,
    ):
        self.backoff: ExponentialBackoff = backoff or ExponentialBackoff()
        self._consecutive_failures: int = 0
        self._max_consecutive_failures: int = max_consecutive_failures
        self._last_connection_time: Optional[float] = None
        self._total_reconnections: int = 0

    def should_reconnect(self) -> bool:
        """Check if reconnection should be attempted.

        Returns:
            True if failure count is below the absolute limit.
        """
        return self._consecutive_failures < self._max_consecutive_failures

    def record_success(self) -> None:
        """Call when a connection is successfully established.

        Resets backoff and failure counters.
        """
        import time

        if self._consecutive_failures > 0:
            logger.info(
                "Connection restored after %d failures",
                self._consecutive_failures,
            )
        self.backoff.reset()
        self._consecutive_failures = 0
        self._last_connection_time = time.time()

    def record_failure(self) -> None:
        """Call when a connection attempt fails.

        Increments failure counter and total reconnection count.
        """
        self._consecutive_failures += 1
        self._total_reconnections += 1
        logger.warning(
            "Connection failure #%d/%d",
            self._consecutive_failures,
            self._max_consecutive_failures,
        )

    async def wait_before_retry(self) -> bool:
        """Wait for the backoff delay and decide whether to retry.

        Returns:
            True if caller should attempt reconnection, False if max exceeded.
        """
        if not self.should_reconnect():
            logger.error(
                "Max consecutive failures (%d) reached. Giving up.",
                self._max_consecutive_failures,
            )
            return False
        delay = self.backoff.next_delay()
        logger.info("Waiting %.2fs before reconnection attempt...", delay)
        await asyncio.sleep(delay)
        return True

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failures since last success."""
        return self._consecutive_failures

    @property
    def total_reconnections(self) -> int:
        """Total number of reconnection attempts (for metrics)."""
        return self._total_reconnections

    @property
    def is_healthy(self) -> bool:
        """True if no consecutive failures are recorded."""
        return self._consecutive_failures == 0

    @property
    def last_connection_time(self) -> Optional[float]:
        """Unix timestamp of the last successful connection, or None."""
        return self._last_connection_time

    def __repr__(self) -> str:
        return (
            f"ReconnectionHandler(failures={self._consecutive_failures}/"
            f"{self._max_consecutive_failures}, "
            f"total_reconnections={self._total_reconnections}, "
            f"healthy={self.is_healthy})"
        )

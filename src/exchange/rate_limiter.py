"""
Crypto Trading Bot - Rate Limiter
==================================

Token-bucket rate limiter for exchange API calls.

Features
--------
- Configurable rate (default 10 requests / second)
- Burst capacity of 20 requests
- Thread-safe via asyncio.Lock
- Automatic 429 retry with exponential backoff
- Handles Binance-specific rate-limit headers

Usage
-----
    limiter = RateLimiter(rate=10, burst=20)
    async with limiter:
        response = await client.get(...)
    # or
    result = await limiter.request(api_call, arg1, arg2)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Binance rate-limit retry defaults
# ---------------------------------------------------------------------------
_DEFAULT_MAX_RETRIES: int = 5
_DEFAULT_BASE_BACKOFF: float = 1.0  # seconds
_DEFAULT_MAX_BACKOFF: float = 60.0  # seconds
_DEFAULT_BACKOFF_JITTER: float = 0.1  # fraction


class RateLimiter:
    """Async token-bucket rate limiter with 429 retry logic.

    Parameters
    ----------
    rate:
        Number of tokens added to the bucket per second (default 10).
    burst:
        Maximum bucket capacity, i.e. the largest burst allowed (default 20).
    max_retries:
        How many times to retry a request that returns HTTP 429.
    base_backoff:
        Initial backoff in seconds for the first 429 retry.
    max_backoff:
        Cap on exponential backoff between retries.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        rate: float = 10.0,
        burst: int = 20,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_backoff: float = _DEFAULT_BASE_BACKOFF,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")

        self._rate: float = float(rate)
        self._capacity: int = int(burst)
        self._max_retries: int = max_retries
        self._base_backoff: float = base_backoff
        self._max_backoff: float = max_backoff

        # Token-bucket state
        self._tokens: float = float(self._capacity)
        self._last_update: float = time.monotonic()

        # Async lock protects _tokens and _last_update
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RateLimiter(rate={self._rate}, burst={self._capacity}, "
            f"tokens={self._tokens:.2f})"
        )

    # ------------------------------------------------------------------ #
    # Token-bucket internals
    # ------------------------------------------------------------------ #

    def _add_tokens(self) -> None:
        """Refill bucket based on elapsed time since last update."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._last_update = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def _wait_for_token(self) -> None:
        """Block until at least one token is available."""
        while self._tokens < 1.0:
            deficit = 1.0 - self._tokens
            wait_time = deficit / self._rate
            logger.debug("Rate limiter waiting %.3f s for token", wait_time)
            await asyncio.sleep(wait_time)
            self._add_tokens()
        self._tokens -= 1.0

    # ------------------------------------------------------------------ #
    # Core API
    # ------------------------------------------------------------------ #

    async def acquire(self) -> None:
        """Acquire one token from the bucket, waiting if necessary."""
        async with self._lock:
            self._add_tokens()
            if self._tokens < 1.0:
                await self._wait_for_token()
            else:
                self._tokens -= 1.0

    async def request(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Acquire a token, call *fn*, and retry on HTTP 429.

        Parameters
        ----------
        fn:
            Async callable to invoke (typically an httpx request method).
        *args, **kwargs:
            Forwarded to *fn*.

        Returns
        -------
        The result of ``await fn(*args, **kwargs)``.

        Raises
        ------
        httpx.HTTPStatusError
            On 4xx / 5xx that is NOT a 429 (after exhausting retries).
        httpx.NetworkError
            On persistent network failures.
        """
        last_exception: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            await self.acquire()

            try:
                result = await fn(*args, **kwargs)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code

                # IP ban (418) - treat as fatal, do NOT retry
                if status_code == 418:
                    logger.critical(
                        "Binance IP ban (418) on attempt %d/%d — aborting",
                        attempt,
                        self._max_retries,
                    )
                    raise

                # Rate limited (429) - retry with exponential backoff
                if status_code == 429:
                    retry_after_header = exc.response.headers.get("Retry-After")
                    if retry_after_header is not None:
                        try:
                            backoff = float(retry_after_header)
                        except (ValueError, TypeError):
                            backoff = self._backoff_for_attempt(attempt)
                    else:
                        backoff = self._backoff_for_attempt(attempt)

                    logger.warning(
                        "HTTP 429 on attempt %d/%d — backing off %.2f s",
                        attempt,
                        self._max_retries,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    last_exception = exc
                    continue

                # All other 4xx / 5xx - do not retry
                logger.error(
                    "HTTP %d on attempt %d/%d — not retrying",
                    status_code,
                    attempt,
                    self._max_retries,
                )
                raise

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                # Transient network issue - retry with backoff
                backoff = self._backoff_for_attempt(attempt)
                logger.warning(
                    "Network error on attempt %d/%d — backing off %.2f s: %s",
                    attempt,
                    self._max_retries,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                last_exception = exc
                continue

            # Success
            return result

        # Exhausted retries
        raise last_exception if last_exception is not None else RuntimeError(
            "Rate limiter exhausted all retries with no exception captured"
        )

    # ------------------------------------------------------------------ #
    # Backoff helpers
    # ------------------------------------------------------------------ #

    def _backoff_for_attempt(self, attempt: int) -> float:
        """Exponential backoff with jitter for the given attempt number."""
        backoff = self._base_backoff * (2 ** (attempt - 1))
        jitter = backoff * _DEFAULT_BACKOFF_JITTER * (2 * random.random() - 1)
        return min(backoff + jitter, self._max_backoff)

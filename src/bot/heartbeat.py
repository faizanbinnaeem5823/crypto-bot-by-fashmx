"""
Background heartbeat task.

Sends a periodic heartbeat to the StateManager so external health-checkers
can verify the bot is alive.  Runs as a cancellable asyncio task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from state.state_manager import StateManager

logger = logging.getLogger(__name__)


async def heartbeat_loop(
    state_manager: StateManager,
    interval_sec: int = 30,
) -> None:
    """Send a heartbeat record every *interval_sec* seconds.

    This is a long-running coroutine intended to be launched with
    ``asyncio.create_task()``.  It cleanly exits on cancellation.

    Parameters
    ----------
    state_manager :
        DuckDB-backed state manager with a ``heartbeat()`` method.
    interval_sec :
        Seconds between heartbeats (default 30).
    """
    bot_id = getattr(state_manager, "bot_id", "unknown")
    logger.info(
        "[%s] Heartbeat loop started (interval=%ds)",
        bot_id,
        interval_sec,
    )

    while True:
        try:
            state_manager.heartbeat()
            logger.debug(
                "[%s] Heartbeat sent at %s",
                bot_id,
                datetime.now(timezone.utc).isoformat(),
            )
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("[%s] Heartbeat loop cancelled — exiting", bot_id)
            break
        except Exception as exc:
            logger.error("[%s] Heartbeat error: %s", bot_id, exc)
            await asyncio.sleep(interval_sec)

    logger.info("[%s] Heartbeat loop stopped", bot_id)

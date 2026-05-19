"""Cross-bot position awareness via Redis.

Both bots (A and B) publish their positions to Redis. Each bot reads
the other's positions to avoid conflicting trades on the same symbol.
Kill signals are also propagated across bots for emergency shutdown.

All timestamps are UTC timezone-aware.
"""

import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    import redis.asyncio as aioredis

    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)


class CrossBotState:
    """Shared state between Bot A and Bot B via Redis.

    Each bot publishes its positions to Redis. Both bots read
    to avoid conflicting positions on the same symbol.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        bot_id: str = "bot_a",
    ):
        self.bot_id = bot_id
        self.redis: Optional[Any] = None
        if REDIS_AVAILABLE:
            try:
                self.redis = aioredis.from_url(redis_url, decode_responses=True)
            except Exception as exc:
                logger.warning("Redis not available: %s", exc)
        self.prefix = "cryptobot:position"
        self.kill_channel = "cryptobot:kill"

    # ------------------------------------------------------------------
    # Position publishing / reading
    # ------------------------------------------------------------------

    async def publish_position(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        """Publish position to shared Redis state."""
        if not self.redis:
            return
        data = {
            "bot_id": self.bot_id,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        key = f"{self.prefix}:{self.bot_id}:{symbol.replace('/', '_')}"
        await self.redis.setex(key, 3600, json.dumps(data))  # 1h TTL
        logger.info("Published position: %s %s %s", symbol, side, quantity)

    async def get_other_bot_positions(self, other_bot_id: str) -> List[Dict]:
        """Get positions published by the other bot."""
        if not self.redis:
            return []
        pattern = f"{self.prefix}:{other_bot_id}:*"
        keys = await self.redis.keys(pattern)
        positions: List[Dict] = []
        for key in keys:
            raw = await self.redis.get(key)
            if raw:
                positions.append(json.loads(raw))
        return positions

    async def has_conflicting_position(
        self, symbol: str, other_bot_id: str
    ) -> bool:
        """Check if other bot has an open position on same symbol."""
        positions = await self.get_other_bot_positions(other_bot_id)
        sym_normalized = symbol.replace("/", "_")
        for pos in positions:
            if pos.get("symbol", "").replace("/", "_") == sym_normalized:
                return True
        return False

    # ------------------------------------------------------------------
    # Cross-bot kill propagation
    # ------------------------------------------------------------------

    async def subscribe_kill(self, callback: Callable) -> None:
        """Subscribe to kill channel for cross-bot kill propagation."""
        if not self.redis:
            return
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.kill_channel)
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                logger.critical(
                    "Kill signal received from %s: %s",
                    data.get("bot_id"),
                    data.get("reason"),
                )
                await callback(data)

    async def publish_kill(self, reason: str) -> None:
        """Publish kill signal to all bots."""
        if not self.redis:
            return
        data = {
            "bot_id": self.bot_id,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.publish(self.kill_channel, json.dumps(data))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.redis:
            await self.redis.close()

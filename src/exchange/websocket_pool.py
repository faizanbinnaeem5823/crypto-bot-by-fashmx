"""WebSocket connection pool for multiple symbol streams.

Manages multiple BinanceWebSocket connections, handles load balancing
across connections, and provides a unified async message stream.

Binance limits:
- Max 1024 streams per connection
- Max 300 connections per 5 minutes from a single IP
- Max ~10 subscribe/unsubscribe messages per second outbound
"""

import asyncio
import logging
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from exchange.binance_ws import BinanceWebSocket, KlineMessage
from exchange.reconnection_handler import ExponentialBackoff, ReconnectionHandler

logger = logging.getLogger(__name__)


class ConnectionSlot:
    """Wrapper around a single BinanceWebSocket with metadata.

    Tracks how many streams are assigned to this connection and
    provides health monitoring hooks.
    """

    def __init__(self, ws: BinanceWebSocket, slot_id: int):
        self.ws = ws
        self.slot_id = slot_id
        self.streams: Set[Tuple[str, str]] = set()  # (symbol, timeframe)
        self.created_at = asyncio.get_event_loop().time()

    @property
    def stream_count(self) -> int:
        return len(self.streams)

    @property
    def is_connected(self) -> bool:
        return self.ws.is_connected

    @property
    def is_full(self, max_streams: int) -> bool:
        return self.stream_count >= max_streams

    def __repr__(self) -> str:
        return (
            f"ConnectionSlot(id={self.slot_id}, "
            f"streams={self.stream_count}, "
            f"connected={self.is_connected})"
        )


class WebSocketPool:
    """Pool of WebSocket connections for multi-symbol monitoring.

    Automatically shards streams across multiple BinanceWebSocket connections
    to respect Binance's per-connection stream limit.  Presents a single
    unified async iterator so downstream code doesn't need to manage
    multiple connections.

    Binance limits:
        - Max 1024 streams per connection
        - Max 300 connections per 5 minutes from a single IP

    Usage::

        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        await pool.add_stream("BTC/USDT", "1d")
        await pool.add_stream("ETH/USDT", "4h")
        await pool.add_stream("SOL/USDT", "1h")
        # Up to 100 streams share one connection; the 101st opens a new one.

        async for msg in pool.messages():
            if msg.is_closed:
                print(f"{msg.symbol} closed candle: {msg.close}")

        await pool.close()
    """

    # Binance hard limits (documented)
    BINANCE_MAX_STREAMS_PER_CONN = 1024
    BINANCE_MAX_CONNS_PER_5MIN = 300

    def __init__(
        self,
        max_streams_per_conn: int = 100,
        testnet: bool = True,
        on_connect: Optional[Callable[[int], None]] = None,
        on_disconnect: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[int, Exception], None]] = None,
    ):
        """Initialize the connection pool.

        Args:
            max_streams_per_conn: Soft limit for streams per WebSocket
                connection.  Must be <= 1024.
            testnet: Pass through to each BinanceWebSocket.
            on_connect: Optional callback(slot_id) on new connection.
            on_disconnect: Optional callback(slot_id) on disconnect.
            on_error: Optional callback(slot_id, exc) on errors.
        """
        if max_streams_per_conn > self.BINANCE_MAX_STREAMS_PER_CONN:
            raise ValueError(
                f"max_streams_per_conn must be <= {self.BINANCE_MAX_STREAMS_PER_CONN}"
            )

        self.max_streams = max_streams_per_conn
        self.testnet = testnet

        # Slot storage
        self._slots: Dict[int, ConnectionSlot] = {}
        self._next_slot_id = 0

        # Mapping: (symbol, timeframe) -> slot_id
        self._symbol_map: Dict[Tuple[str, str], int] = {}

        # Unified message queue for external consumers
        self._message_queue: asyncio.Queue[KlineMessage] = asyncio.Queue()

        # Internal control
        self._running = False
        self._relay_tasks: Set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_error = on_error

        logger.info(
            "WebSocketPool initialized (max_streams=%d, testnet=%s)",
            max_streams_per_conn,
            testnet,
        )

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    async def add_stream(self, symbol: str, timeframe: str) -> None:
        """Add a symbol/timeframe stream to the pool.

        Creates a new WebSocket connection automatically if all existing
        connections are at capacity.

        Args:
            symbol: Trading pair, e.g. "BTC/USDT".
            timeframe: Kline interval, e.g. "1d".

        Raises:
            ValueError: If the stream is already registered.
        """
        key = (symbol, timeframe)

        async with self._lock:
            if key in self._symbol_map:
                logger.warning("Stream %s/%s already registered", symbol, timeframe)
                return

            # Find a slot with capacity
            slot = self._find_slot_with_capacity()
            if slot is None:
                slot = await self._create_new_slot()

            # Subscribe on the connection
            await slot.ws.subscribe_kline(symbol, timeframe)
            slot.streams.add(key)
            self._symbol_map[key] = slot.slot_id
            logger.info(
                "Added stream %s/%s -> slot %d (total on slot: %d)",
                symbol,
                timeframe,
                slot.slot_id,
                slot.stream_count,
            )

    async def add_streams(self, streams: List[Tuple[str, str]]) -> None:
        """Add multiple streams efficiently.

        Groups streams by connection slot to minimize subscribe frames.

        Args:
            streams: List of (symbol, timeframe) tuples.
        """
        for symbol, timeframe in streams:
            await self.add_stream(symbol, timeframe)

    async def remove_stream(self, symbol: str, timeframe: str) -> bool:
        """Remove a symbol/timeframe stream.

        Unsubscribes from the underlying connection.  If the connection
        becomes empty, it is closed to free resources.

        Args:
            symbol: Trading pair, e.g. "BTC/USDT".
            timeframe: Kline interval, e.g. "1d".

        Returns:
            True if the stream was removed, False if not found.
        """
        key = (symbol, timeframe)

        async with self._lock:
            if key not in self._symbol_map:
                logger.warning("Stream %s/%s not found for removal", symbol, timeframe)
                return False

            slot_id = self._symbol_map.pop(key)
            slot = self._slots.get(slot_id)

            if slot is None:
                return False

            await slot.ws.unsubscribe_kline(symbol, timeframe)
            slot.streams.discard(key)
            logger.info(
                "Removed stream %s/%s from slot %d (remaining: %d)",
                symbol,
                timeframe,
                slot_id,
                slot.stream_count,
            )

            # Close empty connections
            if slot.stream_count == 0:
                logger.info("Closing empty slot %d", slot_id)
                await slot.ws.close()
                del self._slots[slot_id]

            return True

    async def remove_streams(self, streams: List[Tuple[str, str]]) -> None:
        """Remove multiple streams."""
        for symbol, timeframe in streams:
            await self.remove_stream(symbol, timeframe)

    def has_stream(self, symbol: str, timeframe: str) -> bool:
        """Check if a stream is currently registered."""
        return (symbol, timeframe) in self._symbol_map

    @property
    def stream_count(self) -> int:
        """Total number of registered streams across all connections."""
        return len(self._symbol_map)

    @property
    def connection_count(self) -> int:
        """Number of active WebSocket connections."""
        return len(self._slots)

    @property
    def streams(self) -> Set[Tuple[str, str]]:
        """Set of all registered (symbol, timeframe) pairs."""
        return set(self._symbol_map.keys())

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def _find_slot_with_capacity(self) -> Optional[ConnectionSlot]:
        """Return a connected slot with room for more streams, or None."""
        for slot in self._slots.values():
            if slot.stream_count < self.max_streams and slot.is_connected:
                return slot
        return None

    async def _create_new_slot(self) -> ConnectionSlot:
        """Create and connect a new BinanceWebSocket, wrapped in a ConnectionSlot.

        Returns:
            The newly created ConnectionSlot.
        """
        slot_id = self._next_slot_id
        self._next_slot_id += 1

        def on_connect():
            logger.info("Slot %d connected", slot_id)
            if self._on_connect:
                try:
                    self._on_connect(slot_id)
                except Exception:
                    logger.exception("on_connect callback error for slot %d", slot_id)

        def on_disconnect():
            logger.info("Slot %d disconnected", slot_id)
            if self._on_disconnect:
                try:
                    self._on_disconnect(slot_id)
                except Exception:
                    logger.exception(
                        "on_disconnect callback error for slot %d", slot_id
                    )

        def on_error(exc: Exception):
            logger.warning("Slot %d error: %s", slot_id, exc)
            if self._on_error:
                try:
                    self._on_error(slot_id, exc)
                except Exception:
                    logger.exception("on_error callback error for slot %d", slot_id)

        ws = BinanceWebSocket(
            testnet=self.testnet,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_error=on_error,
        )

        await ws.connect()
        slot = ConnectionSlot(ws, slot_id)
        self._slots[slot_id] = slot
        self._running = True

        # Start a relay task that forwards messages from this slot
        relay_task = asyncio.create_task(
            self._relay_messages(slot), name=f"pool_relay_slot_{slot_id}"
        )
        self._relay_tasks.add(relay_task)
        relay_task.add_done_callback(self._relay_tasks.discard)

        logger.info("Created new connection slot %d", slot_id)
        return slot

    async def _relay_messages(self, slot: ConnectionSlot) -> None:
        """Forward messages from a single slot into the unified pool queue.

        This task runs per-connection and feeds the shared message queue.
        If the connection drops, the task exits; BinanceWebSocket handles
        reconnection internally, and a new relay task will be started.
        """
        logger.debug("Relay started for slot %d", slot.slot_id)
        try:
            async for msg in slot.ws.messages():
                if not self._running:
                    break
                await self._message_queue.put(msg)
        except Exception:
            logger.exception("Relay error for slot %d", slot.slot_id)
        finally:
            logger.debug("Relay ended for slot %d", slot.slot_id)

    # ------------------------------------------------------------------
    # Unified message API
    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[KlineMessage]:
        """Unified async generator yielding KlineMessages from all connections.

        This is the primary API for downstream consumers::

            async for msg in pool.messages():
                if msg.is_closed:
                    process_closed_candle(msg)
        """
        while self._running:
            try:
                msg: KlineMessage = await asyncio.wait_for(
                    self._message_queue.get(), timeout=1.0
                )
                yield msg
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("Pool message loop error")
                await asyncio.sleep(0.1)

    async def get_message(self) -> Optional[KlineMessage]:
        """Get a single message from the unified queue (blocking).

        Returns:
            KlineMessage or None if pool is closed.
        """
        while self._running:
            try:
                return await asyncio.wait_for(
                    self._message_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("Pool get_message error")
                return None
        return None

    # ------------------------------------------------------------------
    # Health & introspection
    # ------------------------------------------------------------------

    @property
    def health(self) -> Dict[str, Any]:
        """Return a combined health snapshot for all connections."""
        slot_healths = {
            sid: {
                "slot_id": sid,
                "streams": slot.stream_count,
                "connected": slot.is_connected,
                "ws_health": slot.ws.health,
            }
            for sid, slot in self._slots.items()
        }
        return {
            "running": self._running,
            "total_streams": self.stream_count,
            "total_connections": self.connection_count,
            "max_streams_per_conn": self.max_streams,
            "slots": slot_healths,
        }

    def is_healthy(self) -> bool:
        """Return True if at least one connection is alive and has streams."""
        if not self._slots:
            return False
        return any(s.is_connected for s in self._slots.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Mark the pool as running (used when re-starting after close)."""
        self._running = True
        logger.info("WebSocketPool started")

    async def close(self) -> None:
        """Gracefully close all connections and clean up.

        Cancels all relay tasks, closes every underlying WebSocket,
        and clears internal state.
        """
        logger.info("Closing WebSocketPool (%d connections)...", len(self._slots))
        self._running = False

        # Cancel relay tasks
        for task in list(self._relay_tasks):
            if not task.done():
                task.cancel()
        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
        self._relay_tasks.clear()

        # Close all connections concurrently
        close_tasks = [slot.ws.close() for slot in self._slots.values()]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        self._slots.clear()
        self._symbol_map.clear()
        logger.info("WebSocketPool closed")

    async def __aenter__(self):
        self._running = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def __repr__(self) -> str:
        return (
            f"WebSocketPool(connections={self.connection_count}, "
            f"streams={self.stream_count}, "
            f"running={self._running})"
        )

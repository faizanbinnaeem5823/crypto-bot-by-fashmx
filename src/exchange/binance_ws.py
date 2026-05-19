"""Binance WebSocket client for real-time kline (candlestick) streams.

Features:
- Subscribes to kline streams for multiple symbols/timeframes
- Auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, max 60s)
- Heartbeat/ping-pong handling
- Message parsing into pandas DataFrames
- Connection health monitoring
- Graceful shutdown
"""

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

import pandas as pd
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from exchange.reconnection_handler import ExponentialBackoff, ReconnectionHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class KlineMessage:
    """Parsed kline/candlestick message from Binance WebSocket stream.

    Attributes:
        symbol: Trading pair symbol (e.g. "BTCUSDT")
        timeframe: Kline interval (e.g. "1d", "4h", "1h")
        open_time: Candle open timestamp (UTC)
        close_time: Candle close timestamp (UTC)
        open: Opening price
        high: Highest price during the interval
        low: Lowest price during the interval
        close: Closing (current) price
        volume: Base asset volume
        quote_volume: Quote asset volume
        trades: Number of trades in the interval
        is_closed: True when the candle has fully closed
    """

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    is_closed: bool  # True if candle is complete

    def to_series(self) -> pd.Series:
        """Convert to a pandas Series for easy DataFrame concatenation."""
        return pd.Series(
            {
                "open_time": self.open_time,
                "close_time": self.close_time,
                "open": self.open,
                "high": self.high,
                "low": self.low,
                "close": self.close,
                "volume": self.volume,
                "quote_volume": self.quote_volume,
                "trades": self.trades,
                "is_closed": self.is_closed,
            },
            name=(self.symbol, self.timeframe),
        )

    def to_dataframe_row(self) -> pd.DataFrame:
        """Convert to a single-row DataFrame."""
        return pd.DataFrame(
            [
                {
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "open_time": self.open_time,
                    "close_time": self.close_time,
                    "open": self.open,
                    "high": self.high,
                    "low": self.low,
                    "close": self.close,
                    "volume": self.volume,
                    "quote_volume": self.quote_volume,
                    "trades": self.trades,
                    "is_closed": self.is_closed,
                }
            ]
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_symbol(symbol: str) -> str:
    """Convert human-readable symbol to Binance stream format.

    Examples:
        "BTC/USDT" -> "btcusdt"
        "ETH/USDT" -> "ethusdt"
        "BTCUSDT"  -> "btcusdt"
    """
    return symbol.replace("/", "").replace("-", "").lower()


def _ms_to_utc(ms: int) -> datetime:
    """Convert millisecond timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class BinanceWebSocket:
    """Binance WebSocket client for kline (candlestick) streams.

    Manages a single WebSocket connection to Binance, supports multiple
    kline subscriptions, auto-reconnect with exponential backoff, heartbeat
    ping/pong monitoring, and graceful shutdown.

    Usage::

        ws = BinanceWebSocket(testnet=True)
        await ws.connect()
        await ws.subscribe_kline("BTC/USDT", "1d")
        await ws.subscribe_kline("ETH/USDT", "4h")
        async for msg in ws.messages():
            if msg.is_closed:
                print(f"{msg.symbol} {msg.timeframe} closed at {msg.close}")
        await ws.close()
    """

    # --- Binance stream endpoints ------------------------------------------------
    WS_BASE_TESTNET = "wss://testnet.binance.vision/ws"
    WS_BASE_LIVE = "wss://stream.binance.com:9443/ws"
    WS_BASE_LIVE_443 = "wss://stream.binance.com/ws"  # Port 443 (firewall-friendly)

    # --- Timing constants --------------------------------------------------------
    HEARTBEAT_INTERVAL = 30  # seconds between ping frames
    PONG_TIMEOUT = 60  # seconds to wait for pong before reconnect
    MAX_PING_WITHOUT_PONG = 2  # max unanswered pings before forced reconnect
    RECEIVE_TIMEOUT = 300  # max seconds without any message before reconnect
    SUBSCRIBE_TIMEOUT = 10  # seconds to wait for subscribe response

    def __init__(
        self,
        testnet: bool = True,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        """Initialize the WebSocket client.

        Args:
            testnet: Use Binance testnet if True, live stream if False.
            on_connect: Optional callback invoked on successful connection.
            on_disconnect: Optional callback invoked on disconnection.
            on_error: Optional callback invoked with exception on errors.
        """
        self.testnet = testnet
        self.base_url = self.WS_BASE_TESTNET if testnet else self.WS_BASE_LIVE_443

        # Connection handle
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

        # Subscriptions tracking: stream_name -> subscribed flag
        self._subscriptions: Set[str] = set()

        # Message queue for external consumers
        self._message_queue: asyncio.Queue[KlineMessage] = asyncio.Queue()

        # Internal control
        self._running = False
        self._connected = False
        self._tasks: Set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

        # Heartbeat state
        self._last_pong_time = 0.0
        self._unanswered_pings = 0

        # Reconnection handler
        self._reconnect_handler = ReconnectionHandler()

        # Callbacks
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_error = on_error

        # Shutdown signal handling
        self._shutdown_event = asyncio.Event()

        logger.info(
            "BinanceWebSocket initialized (testnet=%s, url=%s)",
            testnet,
            self.base_url,
        )

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish WebSocket connection with retry loop.

        Blocks until the connection is successfully established or
        the maximum number of retries is exceeded.  Once connected,
        background tasks for heartbeat and message reading are started.
        """
        self._running = True
        self._shutdown_event.clear()

        while self._running and self._reconnect_handler.should_reconnect():
            try:
                logger.info("Connecting to %s ...", self.base_url)
                self.ws = await websockets.connect(
                    self.base_url,
                    ping_interval=None,  # We manage pings manually
                    ping_timeout=None,
                    close_timeout=10,
                )
                self._connected = True
                self._last_pong_time = time.time()
                self._unanswered_pings = 0
                self._reconnect_handler.record_success()
                logger.info("WebSocket connected to %s", self.base_url)

                if self._on_connect:
                    try:
                        self._on_connect()
                    except Exception:
                        logger.exception("on_connect callback error")

                # Start background tasks
                self._start_background_tasks()
                return  # Success -- exit the retry loop

            except (ConnectionClosed, InvalidStatusCode, OSError) as exc:
                self._reconnect_handler.record_failure()
                logger.warning(
                    "Connection failed (%s): %s",
                    type(exc).__name__,
                    exc,
                )
                if self._on_error:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass

                should_retry = await self._reconnect_handler.wait_before_retry()
                if not should_retry:
                    raise ConnectionError(
                        f"Max reconnection attempts exceeded for {self.base_url}"
                    ) from exc

            except Exception as exc:
                self._reconnect_handler.record_failure()
                logger.exception("Unexpected connection error: %s", exc)
                should_retry = await self._reconnect_handler.wait_before_retry()
                if not should_retry:
                    raise

    def _start_background_tasks(self) -> None:
        """Spawn heartbeat and receive loop tasks."""
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="binance_ws_heartbeat"
        )
        receive_task = asyncio.create_task(
            self._receive_loop(), name="binance_ws_receive"
        )
        self._tasks.add(heartbeat_task)
        self._tasks.add(receive_task)
        heartbeat_task.add_done_callback(self._tasks.discard)
        receive_task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    async def subscribe_kline(self, symbol: str, timeframe: str) -> None:
        """Subscribe to a kline stream for a single symbol/timeframe.

        If already connected, sends the subscribe frame immediately.
        Otherwise the subscription is queued and applied on connect.

        Args:
            symbol: Trading pair, e.g. "BTC/USDT" or "BTCUSDT".
            timeframe: Kline interval, e.g. "1d", "4h", "1h", "15m".

        Raises:
            ConnectionClosed: If the WebSocket is not connected.
        """
        stream_name = f"{_normalize_symbol(symbol)}@kline_{timeframe}"

        async with self._lock:
            self._subscriptions.add(stream_name)

            if self._connected and self.ws is not None:
                await self._send_subscribe([stream_name])
                logger.info("Subscribed to %s", stream_name)
            else:
                logger.debug("Queued subscription to %s (not connected)", stream_name)

    async def subscribe_multiple(self, subscriptions: List[tuple]) -> None:
        """Subscribe to multiple symbol/timeframe pairs at once.

        Args:
            subscriptions: List of (symbol, timeframe) tuples, e.g.
                [("BTC/USDT", "1d"), ("ETH/USDT", "4h")].
        """
        stream_names = [
            f"{_normalize_symbol(sym)}@kline_{tf}" for sym, tf in subscriptions
        ]

        async with self._lock:
            for name in stream_names:
                self._subscriptions.add(name)

            if self._connected and self.ws is not None:
                await self._send_subscribe(stream_names)
                logger.info(
                    "Subscribed to %d streams: %s",
                    len(stream_names),
                    ", ".join(stream_names),
                )
            else:
                logger.debug(
                    "Queued %d subscriptions (not connected)", len(stream_names)
                )

    async def unsubscribe_kline(self, symbol: str, timeframe: str) -> None:
        """Unsubscribe from a kline stream.

        Args:
            symbol: Trading pair, e.g. "BTC/USDT".
            timeframe: Kline interval, e.g. "1d".
        """
        stream_name = f"{_normalize_symbol(symbol)}@kline_{timeframe}"

        async with self._lock:
            self._subscriptions.discard(stream_name)

            if self._connected and self.ws is not None:
                await self._send_unsubscribe([stream_name])
                logger.info("Unsubscribed from %s", stream_name)

    async def _send_subscribe(self, stream_names: List[str]) -> None:
        """Send a SUBSCRIBE frame to Binance."""
        if not self.ws:
            return
        payload = {
            "method": "SUBSCRIBE",
            "params": stream_names,
            "id": int(time.time() * 1000),
        }
        await self.ws.send(json.dumps(payload))

    async def _send_unsubscribe(self, stream_names: List[str]) -> None:
        """Send an UNSUBSCRIBE frame to Binance."""
        if not self.ws:
            return
        payload = {
            "method": "UNSUBSCRIBE",
            "params": stream_names,
            "id": int(time.time() * 1000),
        }
        await self.ws.send(json.dumps(payload))

    async def _resubscribe_all(self) -> None:
        """Re-send all stored subscriptions after reconnect."""
        async with self._lock:
            if self._subscriptions and self.ws is not None:
                await self._send_subscribe(list(self._subscriptions))
                logger.info(
                    "Re-subscribed to %d streams after reconnect",
                    len(self._subscriptions),
                )

    # ------------------------------------------------------------------
    # Public message API
    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[KlineMessage]:
        """Async generator yielding parsed KlineMessage objects.

        Only yields messages where the candle has fully closed
        (``is_closed=True``).  Use this as the primary data source::

            async for msg in ws.messages():
                if msg.is_closed:
                    df.loc[msg.open_time] = msg.close
        """
        while self._running:
            try:
                msg: KlineMessage = await asyncio.wait_for(
                    self._message_queue.get(), timeout=1.0
                )
                if msg.is_closed:
                    yield msg
            except asyncio.TimeoutError:
                # Loop control -- check _running
                continue
            except Exception:
                logger.exception("Error yielding message")
                await asyncio.sleep(0.1)

    async def get_message(self) -> Optional[KlineMessage]:
        """Get a single closed-candle message (blocking).

        Returns:
            KlineMessage if available, None if connection is closed.
        """
        while self._running:
            try:
                msg: KlineMessage = await asyncio.wait_for(
                    self._message_queue.get(), timeout=1.0
                )
                if msg.is_closed:
                    return msg
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("Error getting message")
                return None
        return None

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Continuously read messages from the WebSocket.

        Parses kline events, puts closed candles onto the message queue,
        and handles connection loss by triggering reconnection.
        """
        logger.debug("Receive loop started")
        try:
            while self._running and self.ws is not None:
                try:
                    raw = await asyncio.wait_for(
                        self.ws.recv(), timeout=self.RECEIVE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "No message in %ds -- forcing reconnect",
                        self.RECEIVE_TIMEOUT,
                    )
                    await self._trigger_reconnect()
                    return

                # Reset reconnect handler on any successful receive
                if self._reconnect_handler.consecutive_failures > 0:
                    self._reconnect_handler.record_success()

                self._last_pong_time = time.time()
                self._unanswered_pings = 0

                await self._process_raw_message(raw)

        except ConnectionClosed as exc:
            logger.warning("WebSocket connection closed: %s", exc)
            if self._running:
                await self._trigger_reconnect()
        except Exception as exc:
            logger.exception("Receive loop error: %s", exc)
            if self._on_error:
                try:
                    self._on_error(exc)
                except Exception:
                    pass
            if self._running:
                await self._trigger_reconnect()
        finally:
            logger.debug("Receive loop ended")

    async def _process_raw_message(self, raw: str) -> None:
        """Parse a raw JSON string and route it appropriately."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON message: %.200s", raw)
            return

        # Route by message type
        event_type = data.get("e")

        if event_type == "kline":
            kline = self._parse_kline(data)
            if kline is not None:
                await self._message_queue.put(kline)
        elif event_type is None:
            # Could be a response frame (subscribe/unsubscribe result)
            if "result" in data:
                logger.debug("Response frame: %s", data)
            else:
                logger.debug("Non-event message: %s", data)
        else:
            logger.debug("Unhandled event type '%s': %.200s", event_type, raw)

    def _parse_kline(self, data: dict) -> Optional[KlineMessage]:
        """Parse a kline WebSocket message into KlineMessage.

        Expected format::

            {
                "e": "kline",
                "E": 1672531200000,
                "s": "BTCUSDT",
                "k": {
                    "t": 1672531200000,
                    "T": 1672617599999,
                    "s": "BTCUSDT",
                    "i": "1d",
                    "o": "16500",
                    "h": "16800",
                    "l": "16450",
                    "c": "16750",
                    "v": "12000",
                    "n": 15000,
                    "x": true
                }
            }

        Returns:
            KlineMessage or None if parsing fails.
        """
        try:
            k = data.get("k", {})
            if not k:
                logger.warning("Kline message missing 'k' field: %s", data)
                return None

            return KlineMessage(
                symbol=k.get("s", data.get("s", "UNKNOWN")),
                timeframe=k.get("i", "unknown"),
                open_time=_ms_to_utc(k["t"]),
                close_time=_ms_to_utc(k["T"]),
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
                quote_volume=float(k.get("q", 0)),
                trades=int(k.get("n", 0)),
                is_closed=bool(k.get("x", False)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Failed to parse kline: %s -- %s", exc, data)
            return None

    # ------------------------------------------------------------------
    # Heartbeat / ping-pong
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send periodic ping frames and monitor pong responses.

        - Sends a ping frame every HEARTBEAT_INTERVAL seconds.
        - Tracks unanswered pings.
        - If no pong received within PONG_TIMEOUT seconds, or if
          MAX_PING_WITHOUT_PONG pings go unanswered, forces a reconnect.
        """
        logger.debug("Heartbeat loop started (interval=%ds)", self.HEARTBEAT_INTERVAL)
        while self._running and self.ws is not None:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

            if not self._running or self.ws is None:
                break

            # Check if we've lost the connection (no pong for too long)
            time_since_pong = time.time() - self._last_pong_time
            if time_since_pong > self.PONG_TIMEOUT:
                logger.warning(
                    "No pong in %.0fs (timeout=%ds) -- forcing reconnect",
                    time_since_pong,
                    self.PONG_TIMEOUT,
                )
                await self._trigger_reconnect()
                return

            if self._unanswered_pings >= self.MAX_PING_WITHOUT_PONG:
                logger.warning(
                    "%d unanswered pings -- forcing reconnect",
                    self._unanswered_pings,
                )
                await self._trigger_reconnect()
                return

            try:
                pong_waiter = await self.ws.ping()
                self._unanswered_pings += 1
                logger.debug("Ping sent (#%d)", self._unanswered_pings)

                # Wait for pong asynchronously so we don't block
                asyncio.create_task(self._wait_pong(pong_waiter))
            except ConnectionClosed:
                logger.warning("Ping failed: connection closed")
                if self._running:
                    await self._trigger_reconnect()
                return
            except Exception as exc:
                logger.warning("Ping error: %s", exc)

    async def _wait_pong(self, pong_waiter: asyncio.Future) -> None:
        """Wait for a pong response and update state."""
        try:
            await asyncio.wait_for(pong_waiter, timeout=self.PONG_TIMEOUT)
            self._last_pong_time = time.time()
            self._unanswered_pings = 0
            logger.debug("Pong received")
        except asyncio.TimeoutError:
            logger.debug("Pong wait timed out")
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.debug("Pong wait error: %s", exc)

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _trigger_reconnect(self) -> None:
        """Initiate reconnection sequence.

        Closes the current connection (if any), records a failure,
        waits the backoff delay, and re-opens the connection.
        After reconnecting, re-subscribes to all previously registered streams.
        """
        if not self._running:
            return

        logger.info("Triggering reconnection...")
        self._connected = False

        # Close current connection
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        if self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:
                pass

        # Cancel background tasks
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Record failure and wait before retry
        self._reconnect_handler.record_failure()
        should_retry = await self._reconnect_handler.wait_before_retry()
        if not should_retry:
            logger.error("Max reconnection attempts reached -- stopping")
            self._running = False
            self._shutdown_event.set()
            return

        # Attempt to reconnect
        try:
            await self.connect()
            await self._resubscribe_all()
        except Exception as exc:
            logger.error("Reconnection failed: %s", exc)
            # The connect() method has its own retry loop, so if we get here
            # it means we've exhausted retries.  Propagate to shutdown.
            self._running = False
            self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Gracefully close the WebSocket connection and clean up.

        Cancels all background tasks, closes the socket, and clears
        internal state.  This method is safe to call multiple times.
        """
        logger.info("Closing WebSocket connection...")
        self._running = False
        self._connected = False
        self._shutdown_event.set()

        # Cancel all background tasks
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close the socket
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        logger.info("WebSocket connection closed")

    async def wait_closed(self) -> None:
        """Block until the connection is fully closed or shutdown is signalled."""
        await self._shutdown_event.wait()

    # ------------------------------------------------------------------
    # Properties & introspection
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True if the WebSocket is currently connected."""
        return self._connected and self.ws is not None

    @property
    def is_running(self) -> bool:
        """True if the client is still running (not closed)."""
        return self._running

    @property
    def subscription_count(self) -> int:
        """Number of active stream subscriptions."""
        return len(self._subscriptions)

    @property
    def subscriptions(self) -> Set[str]:
        """Set of subscribed stream names."""
        return set(self._subscriptions)

    @property
    def health(self) -> Dict[str, Any]:
        """Return a snapshot of connection health metrics."""
        return {
            "connected": self.is_connected,
            "running": self.is_running,
            "subscriptions": self.subscription_count,
            "consecutive_failures": self._reconnect_handler.consecutive_failures,
            "total_reconnections": self._reconnect_handler.total_reconnections,
            "unanswered_pings": self._unanswered_pings,
            "seconds_since_pong": (
                time.time() - self._last_pong_time if self._last_pong_time else None
            ),
        }

    def __repr__(self) -> str:
        return (
            f"BinanceWebSocket(connected={self.is_connected}, "
            f"subscriptions={self.subscription_count}, "
            f"failures={self._reconnect_handler.consecutive_failures})"
        )


# ---------------------------------------------------------------------------
# Convenience: create client from symbol/timeframe list
# ---------------------------------------------------------------------------


async def create_binance_ws(
    subscriptions: List[tuple],
    testnet: bool = True,
) -> BinanceWebSocket:
    """Create a connected BinanceWebSocket with subscriptions pre-configured.

    Args:
        subscriptions: List of (symbol, timeframe) tuples.
        testnet: Use testnet if True.

    Returns:
        Connected and subscribed BinanceWebSocket instance.
    """
    ws = BinanceWebSocket(testnet=testnet)
    await ws.connect()
    await ws.subscribe_multiple(subscriptions)
    return ws

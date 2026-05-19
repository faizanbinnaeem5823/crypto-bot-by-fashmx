"""
Comprehensive integration tests for exchange modules.

Covers:
- BinanceWebSocket   : connect, subscribe, kline parsing, reconnection, close
- WebSocketPool      : add_stream, messages, slot management, relay
- RateLimiter        : acquire, request, 429 retry, IP ban, network errors
- BinanceClient      : symbol normalization, HMAC signing, place_order, get_balance
- PaperBroker        : init, buy, sell, PnL tracking, trade history
- ReconnectionHandler: exponential backoff, max failures, reset, wait_before_retry
"""

import asyncio
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pandas as pd
import pytest

# Add src/ to path so 'exchange.*' imports resolve (binance_ws uses absolute imports)
sys.path.insert(0, "src")

from exchange.binance_ws import BinanceWebSocket, KlineMessage, _normalize_symbol
from exchange.binance_client import BinanceAuth, AuthMethod, BinanceClient
from exchange.paper_broker import PaperBroker
from exchange.rate_limiter import RateLimiter
from exchange.reconnection_handler import ExponentialBackoff, ReconnectionHandler
from exchange.websocket_pool import ConnectionSlot, WebSocketPool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_kline_message():
    """Return a raw Binance kline WebSocket message."""
    return {
        "e": "kline",
        "E": 1672531200000,
        "s": "BTCUSDT",
        "k": {
            "t": 1672531200000,
            "T": 1672617599999,
            "s": "BTCUSDT",
            "i": "1d",
            "o": "16500.00",
            "h": "16800.00",
            "l": "16450.00",
            "c": "16750.00",
            "v": "12000.00",
            "q": "200000000",
            "n": 15000,
            "x": True,
        },
    }


@pytest.fixture
def sample_kline_message_eth():
    """Return a raw ETHUSDT kline message (candle not closed)."""
    return {
        "e": "kline",
        "E": 1672531200000,
        "s": "ETHUSDT",
        "k": {
            "t": 1672531200000,
            "T": 1672617599999,
            "s": "ETHUSDT",
            "i": "4h",
            "o": "1200.00",
            "h": "1250.00",
            "l": "1190.00",
            "c": "1240.00",
            "v": "5000.00",
            "q": "6000000",
            "n": 8000,
            "x": False,
        },
    }


@pytest.fixture
def sample_response_frame():
    """Return a subscribe/unsubscribe response frame."""
    return {"result": None, "id": 1700000000000}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def async_empty_generator():
    """Empty async generator for mocking ws.messages()."""
    return
    yield  # Make it a generator


# ---------------------------------------------------------------------------
# TestBinanceWebSocket
# ---------------------------------------------------------------------------


class TestBinanceWebSocket:
    """Tests for BinanceWebSocket -- connection, subscription, parsing, lifecycle."""

    def test_initialization_testnet(self):
        """WS client init with testnet=True uses testnet URL."""
        ws = BinanceWebSocket(testnet=True)
        assert ws.testnet is True
        assert ws.base_url == "wss://testnet.binance.vision/ws"
        assert ws.is_connected is False
        assert ws.is_running is False
        assert ws.subscription_count == 0

    def test_initialization_live(self):
        """WS client init with testnet=False uses live URL."""
        ws = BinanceWebSocket(testnet=False)
        assert ws.testnet is False
        assert ws.base_url == "wss://stream.binance.com/ws"

    def test_initialization_callbacks(self):
        """Callbacks are stored during initialization."""
        on_connect = MagicMock()
        on_disconnect = MagicMock()
        on_error = MagicMock()
        ws = BinanceWebSocket(
            testnet=True,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_error=on_error,
        )
        assert ws._on_connect is on_connect
        assert ws._on_disconnect is on_disconnect
        assert ws._on_error is on_error

    def test_kline_message_parsing(self, sample_kline_message):
        """_parse_kline correctly parses a valid kline message."""
        ws = BinanceWebSocket(testnet=True)
        result = ws._parse_kline(sample_kline_message)
        assert result is not None
        assert result.symbol == "BTCUSDT"
        assert result.timeframe == "1d"
        assert result.is_closed is True
        assert result.open == 16500.00
        assert result.high == 16800.00
        assert result.low == 16450.00
        assert result.close == 16750.00
        assert result.volume == 12000.00
        assert result.quote_volume == 200000000.0
        assert result.trades == 15000
        assert isinstance(result.open_time, datetime)
        assert result.open_time.tzinfo is not None  # UTC aware

    def test_kline_message_parsing_open_candle(self, sample_kline_message_eth):
        """_parse_kline handles open (not closed) candles correctly."""
        ws = BinanceWebSocket(testnet=True)
        result = ws._parse_kline(sample_kline_message_eth)
        assert result is not None
        assert result.symbol == "ETHUSDT"
        assert result.is_closed is False

    def test_kline_message_parsing_invalid(self):
        """_parse_kline returns None for malformed messages."""
        ws = BinanceWebSocket(testnet=True)
        assert ws._parse_kline({}) is None
        assert ws._parse_kline({"e": "kline"}) is None
        assert ws._parse_kline({"e": "kline", "k": {}}) is None

    def test_kline_to_series(self, sample_kline_message):
        """KlineMessage.to_series returns a properly named pandas Series."""
        ws = BinanceWebSocket(testnet=True)
        kline = ws._parse_kline(sample_kline_message)
        series = kline.to_series()
        assert isinstance(series, pd.Series)
        assert series.name == ("BTCUSDT", "1d")
        assert "open" in series.index
        assert "close" in series.index
        assert series["close"] == 16750.00

    def test_kline_to_dataframe_row(self, sample_kline_message):
        """KlineMessage.to_dataframe_row returns a single-row DataFrame."""
        ws = BinanceWebSocket(testnet=True)
        kline = ws._parse_kline(sample_kline_message)
        df = kline.to_dataframe_row()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_subscribe_kline_when_connected(self):
        """subscribe_kline sends SUBSCRIBE frame when already connected."""
        ws = BinanceWebSocket(testnet=True)
        ws._connected = True
        ws.ws = AsyncMock()
        ws.ws.send = AsyncMock()

        await ws.subscribe_kline("BTC/USDT", "1d")

        ws.ws.send.assert_awaited_once()
        sent_raw = ws.ws.send.await_args[0][0]
        sent_json = json.loads(sent_raw)
        assert sent_json["method"] == "SUBSCRIBE"
        assert "btcusdt@kline_1d" in sent_json["params"]

    @pytest.mark.asyncio
    async def test_subscribe_kline_queued_when_not_connected(self):
        """subscribe_kline queues subscription when not connected."""
        ws = BinanceWebSocket(testnet=True)
        ws._connected = False
        ws.ws = None

        await ws.subscribe_kline("BTC/USDT", "1d")

        assert "btcusdt@kline_1d" in ws._subscriptions
        assert ws.ws is None  # No send attempted

    @pytest.mark.asyncio
    async def test_unsubscribe_kline(self):
        """unsubscribe_kline sends UNSUBSCRIBE frame."""
        ws = BinanceWebSocket(testnet=True)
        ws._connected = True
        ws._subscriptions.add("btcusdt@kline_1d")
        ws.ws = AsyncMock()
        ws.ws.send = AsyncMock()

        await ws.unsubscribe_kline("BTC/USDT", "1d")

        ws.ws.send.assert_awaited_once()
        sent_raw = ws.ws.send.await_args[0][0]
        sent_json = json.loads(sent_raw)
        assert sent_json["method"] == "UNSUBSCRIBE"

    @pytest.mark.asyncio
    async def test_process_raw_message_kline(self, sample_kline_message):
        """_process_raw_message routes kline events to the queue."""
        ws = BinanceWebSocket(testnet=True)
        ws._message_queue = asyncio.Queue()
        raw = json.dumps(sample_kline_message)

        await ws._process_raw_message(raw)

        assert ws._message_queue.qsize() == 1
        msg = await ws._message_queue.get()
        assert msg.symbol == "BTCUSDT"
        assert msg.is_closed is True

    @pytest.mark.asyncio
    async def test_process_raw_message_response_frame(self, sample_response_frame):
        """_process_raw_message handles subscribe/unsubscribe response frames."""
        ws = BinanceWebSocket(testnet=True)
        ws._message_queue = asyncio.Queue()
        raw = json.dumps(sample_response_frame)

        await ws._process_raw_message(raw)

        # Response frames should not produce queue items
        assert ws._message_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_process_raw_message_non_json(self):
        """_process_raw_message gracefully handles non-JSON input."""
        ws = BinanceWebSocket(testnet=True)
        ws._message_queue = asyncio.Queue()

        await ws._process_raw_message("not valid json {{{")
        assert ws._message_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_messages_generator_yields_closed_candles(self, sample_kline_message):
        """messages() async generator yields only closed candles."""
        ws = BinanceWebSocket(testnet=True)
        ws._running = True
        ws._message_queue = asyncio.Queue()
        kline = ws._parse_kline(sample_kline_message)
        await ws._message_queue.put(kline)

        # Stop after yielding one message
        messages = []
        async def _consume():
            async for msg in ws.messages():
                messages.append(msg)
                ws._running = False
                return

        await asyncio.wait_for(_consume(), timeout=2.0)
        assert len(messages) == 1
        assert messages[0].symbol == "BTCUSDT"
        assert messages[0].is_closed is True

    @pytest.mark.asyncio
    async def test_close_sets_running_false(self):
        """close() sets _running to False and clears tasks."""
        ws = BinanceWebSocket(testnet=True)
        ws.ws = AsyncMock()
        ws.ws.close = AsyncMock()
        ws._running = True
        ws._connected = True

        await ws.close()

        assert ws._running is False
        assert ws._connected is False

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """connect() establishes WebSocket and starts background tasks."""
        ws = BinanceWebSocket(testnet=True)
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()

        with patch("websockets.connect", new_callable=AsyncMock, return_value=mock_ws):
            await ws.connect()

        assert ws._connected is True
        assert ws.is_running is True
        assert len(ws._tasks) == 2  # heartbeat + receive
        await ws.close()

    @pytest.mark.asyncio
    async def test_connect_then_retry_then_fail(self):
        """connect() retries on connection failure, eventually raises."""
        from websockets.exceptions import ConnectionClosed

        ws = BinanceWebSocket(testnet=True)
        # Force fast failure -- max 1 consecutive failure allowed
        ws._reconnect_handler = MagicMock()
        ws._reconnect_handler.should_reconnect.return_value = True
        ws._reconnect_handler.wait_before_retry = AsyncMock(return_value=False)
        ws._reconnect_handler.record_failure = MagicMock()

        with patch(
            "websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionClosed(None, None),
        ):
            with pytest.raises(ConnectionError):
                await ws.connect()

    @pytest.mark.asyncio
    async def test_resubscribe_all(self):
        """_resubscribe_all sends all stored subscriptions."""
        ws = BinanceWebSocket(testnet=True)
        ws._subscriptions = {"btcusdt@kline_1d", "ethusdt@kline_4h"}
        ws.ws = AsyncMock()
        ws.ws.send = AsyncMock()

        await ws._resubscribe_all()

        ws.ws.send.assert_awaited_once()
        sent_raw = ws.ws.send.await_args[0][0]
        sent_json = json.loads(sent_raw)
        assert len(sent_json["params"]) == 2
        assert "btcusdt@kline_1d" in sent_json["params"]
        assert "ethusdt@kline_4h" in sent_json["params"]

    @pytest.mark.asyncio
    async def test_trigger_reconnect(self):
        """_trigger_reconnect closes connection, records failure, attempts reconnect."""
        ws = BinanceWebSocket(testnet=True)
        mock_ws = AsyncMock()
        ws.ws = mock_ws
        ws._connected = True
        ws._running = True
        ws._reconnect_handler = MagicMock()
        ws._reconnect_handler.wait_before_retry = AsyncMock(return_value=False)
        ws._reconnect_handler.record_failure = MagicMock()

        await ws._trigger_reconnect()

        mock_ws.close.assert_awaited_once()
        ws._reconnect_handler.record_failure.assert_called_once()

    def test_health_property(self):
        """health property returns a snapshot dict with expected keys."""
        ws = BinanceWebSocket(testnet=True)
        health = ws.health
        assert isinstance(health, dict)
        assert "connected" in health
        assert "running" in health
        assert "subscriptions" in health
        assert "consecutive_failures" in health
        assert "total_reconnections" in health
        assert "unanswered_pings" in health
        assert "seconds_since_pong" in health

    def test_repr(self):
        """__repr__ returns a descriptive string."""
        ws = BinanceWebSocket(testnet=True)
        r = repr(ws)
        assert "BinanceWebSocket" in r
        assert "connected" in r

    @pytest.mark.asyncio
    async def test_wait_pong_success(self):
        """_wait_pong updates last_pong_time on successful pong."""
        ws = BinanceWebSocket(testnet=True)
        ws._unanswered_pings = 1
        # Use a real resolved asyncio.Future
        future = asyncio.get_event_loop().create_future()
        future.set_result(None)

        await ws._wait_pong(future)

        assert ws._unanswered_pings == 0
        assert ws._last_pong_time > 0

    @pytest.mark.asyncio
    async def test_wait_pong_timeout(self):
        """_wait_pong handles timeout without crashing."""
        import asyncio as aio

        ws = BinanceWebSocket(testnet=True)
        ws._unanswered_pings = 1
        mock_future = AsyncMock()

        with patch("asyncio.wait_for", AsyncMock(side_effect=aio.TimeoutError)):
            await ws._wait_pong(mock_future)

        # Unanswered pings should remain unchanged on timeout
        assert ws._unanswered_pings == 1


# ---------------------------------------------------------------------------
# TestWebSocketPool
# ---------------------------------------------------------------------------


class TestWebSocketPool:
    """Tests for WebSocketPool -- multi-connection stream management."""

    @pytest.mark.asyncio
    async def test_pool_initialization(self):
        """Pool initializes with correct defaults."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        assert pool.max_streams == 100
        assert pool.testnet is True
        assert pool.stream_count == 0
        assert pool.connection_count == 0
        assert pool._running is False

    def test_pool_initialization_exceeds_limit(self):
        """Pool rejects max_streams_per_conn above Binance hard limit."""
        with pytest.raises(ValueError):
            WebSocketPool(max_streams_per_conn=2000, testnet=True)

    @pytest.mark.asyncio
    async def test_add_stream_creates_new_slot(self):
        """add_stream creates a new connection slot on first stream."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)

        with patch("exchange.websocket_pool.BinanceWebSocket") as MockWS:
            instance = MockWS.return_value
            instance.connect = AsyncMock()
            instance.subscribe_kline = AsyncMock()
            instance.messages = MagicMock(return_value=async_empty_generator())
            instance.close = AsyncMock()
            await pool.add_stream("BTC/USDT", "1d")

        assert pool.stream_count == 1
        assert pool.connection_count == 1
        assert pool.has_stream("BTC/USDT", "1d") is True
        await pool.close()

    @pytest.mark.asyncio
    async def test_add_duplicate_stream_is_ignored(self):
        """Adding the same stream twice is a no-op."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)

        with patch("exchange.websocket_pool.BinanceWebSocket") as MockWS:
            instance = MockWS.return_value
            instance.connect = AsyncMock()
            instance.subscribe_kline = AsyncMock()
            instance.messages = MagicMock(return_value=async_empty_generator())
            instance.close = AsyncMock()
            await pool.add_stream("BTC/USDT", "1d")
            await pool.add_stream("BTC/USDT", "1d")  # Duplicate

        assert pool.stream_count == 1  # Still just one
        await pool.close()

    @pytest.mark.asyncio
    async def test_remove_stream(self):
        """remove_stream unsubscribes and cleans up empty connections."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)

        with patch("exchange.websocket_pool.BinanceWebSocket") as MockWS:
            instance = MockWS.return_value
            instance.connect = AsyncMock()
            instance.subscribe_kline = AsyncMock()
            instance.unsubscribe_kline = AsyncMock()
            instance.messages = MagicMock(return_value=async_empty_generator())
            instance.close = AsyncMock()
            await pool.add_stream("BTC/USDT", "1d")
            result = await pool.remove_stream("BTC/USDT", "1d")

        assert result is True
        assert pool.stream_count == 0
        assert pool.connection_count == 0
        await pool.close()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_stream(self):
        """remove_stream returns False for unknown streams."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        result = await pool.remove_stream("BTC/USDT", "1d")
        assert result is False

    @pytest.mark.asyncio
    async def test_pool_messages_generator(self):
        """messages() yields from the unified queue."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        pool._running = True
        pool._message_queue = asyncio.Queue()

        kline = KlineMessage(
            symbol="BTCUSDT",
            timeframe="1d",
            open_time=datetime.now(timezone.utc),
            close_time=datetime.now(timezone.utc),
            open=100.0,
            high=110.0,
            low=95.0,
            close=105.0,
            volume=1000.0,
            quote_volume=100000.0,
            trades=100,
            is_closed=True,
        )
        await pool._message_queue.put(kline)

        messages = []
        async def _consume():
            async for msg in pool.messages():
                messages.append(msg)
                pool._running = False
                return

        await asyncio.wait_for(_consume(), timeout=2.0)
        assert len(messages) == 1
        assert messages[0].symbol == "BTCUSDT"

    def test_pool_is_healthy_no_connections(self):
        """is_healthy returns False when no connections exist."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        assert pool.is_healthy() is False

    def test_pool_streams_property(self):
        """streams property returns all registered (symbol, timeframe) pairs."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        # Manually inject a symbol mapping
        pool._symbol_map = {("BTC/USDT", "1d"): 0, ("ETH/USDT", "4h"): 0}
        streams = pool.streams
        assert ("BTC/USDT", "1d") in streams
        assert ("ETH/USDT", "4h") in streams

    @pytest.mark.asyncio
    async def test_pool_close(self):
        """close() stops relay tasks and closes all connections."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        mock_ws = AsyncMock()
        pool._slots[0] = MagicMock()
        pool._slots[0].ws = mock_ws
        pool._running = True

        await pool.close()

        assert pool._running is False
        assert pool.connection_count == 0

    def test_pool_health_property(self):
        """health property returns combined snapshot for all slots."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        health = pool.health
        assert isinstance(health, dict)
        assert "running" in health
        assert "total_streams" in health
        assert "total_connections" in health
        assert "max_streams_per_conn" in health
        assert "slots" in health

    def test_pool_repr(self):
        """__repr__ returns a descriptive string."""
        pool = WebSocketPool(max_streams_per_conn=100, testnet=True)
        r = repr(pool)
        assert "WebSocketPool" in r

    def test_connection_slot_repr(self):
        """ConnectionSlot __repr__ is descriptive."""
        mock_ws = MagicMock()
        slot = ConnectionSlot(mock_ws, slot_id=0)
        r = repr(slot)
        assert "ConnectionSlot" in r
        assert "id=0" in r


# ---------------------------------------------------------------------------
# TestRateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Tests for RateLimiter -- token-bucket with 429 retry."""

    @pytest.mark.asyncio
    async def test_rate_limiter_acquire_within_burst(self):
        """acquire() succeeds immediately when tokens are available."""
        rl = RateLimiter(rate=10.0, burst=5)
        # Should not block -- bucket starts full
        await rl.acquire()
        assert rl._tokens <= 4.0  # One consumed

    @pytest.mark.asyncio
    async def test_rate_limiter_acquire_blocks_when_empty(self):
        """acquire() blocks when bucket is exhausted."""
        rl = RateLimiter(rate=100.0, burst=1)  # 1 token, refills fast
        await rl.acquire()  # Consume the only token
        # Second acquire should need to wait for refill
        start = time.monotonic()
        await rl.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # Fast refill at rate=100

    @pytest.mark.asyncio
    async def test_rate_limiter_request_success(self):
        """request() calls the wrapped function and returns its result."""
        rl = RateLimiter(rate=10.0, burst=10)
        mock_fn = AsyncMock(return_value={"status": "ok", "data": [1, 2, 3]})
        result = await rl.request(mock_fn, "arg1", key="val")
        assert result == {"status": "ok", "data": [1, 2, 3]}
        mock_fn.assert_awaited_once_with("arg1", key="val")

    @pytest.mark.asyncio
    async def test_rate_limiter_request_429_retry_then_success(self):
        """request() retries on HTTP 429 and eventually succeeds."""
        rl = RateLimiter(rate=100.0, burst=10, base_backoff=0.01)
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "0.001"}

        exc = httpx.HTTPStatusError(
            "Too Many Requests", request=MagicMock(), response=mock_response
        )
        mock_fn = AsyncMock(side_effect=[exc, {"status": "recovered"}])
        result = await rl.request(mock_fn)
        assert result == {"status": "recovered"}
        assert mock_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_rate_limiter_request_429_exhausts_retries(self):
        """request() raises last exception after exhausting 429 retries."""
        rl = RateLimiter(rate=100.0, burst=10, max_retries=2, base_backoff=0.001)
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}

        exc = httpx.HTTPStatusError(
            "Too Many Requests", request=MagicMock(), response=mock_response
        )
        mock_fn = AsyncMock(side_effect=[exc, exc])
        with pytest.raises(httpx.HTTPStatusError):
            await rl.request(mock_fn)

    @pytest.mark.asyncio
    async def test_rate_limiter_request_418_ip_ban(self):
        """request() immediately raises on HTTP 418 (IP ban) without retry."""
        rl = RateLimiter(rate=100.0, burst=10)
        mock_response = MagicMock()
        mock_response.status_code = 418

        exc = httpx.HTTPStatusError(
            "IP Ban", request=MagicMock(), response=mock_response
        )
        mock_fn = AsyncMock(side_effect=exc)
        with pytest.raises(httpx.HTTPStatusError):
            await rl.request(mock_fn)
        mock_fn.assert_awaited_once()  # Only one attempt

    @pytest.mark.asyncio
    async def test_rate_limiter_request_network_error_retry(self):
        """request() retries on network errors with backoff."""
        rl = RateLimiter(rate=100.0, burst=10, max_retries=2, base_backoff=0.001)
        mock_fn = AsyncMock(
            side_effect=[httpx.NetworkError("conn reset"), {"status": "ok"}]
        )
        result = await rl.request(mock_fn)
        assert result == {"status": "ok"}
        assert mock_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_rate_limiter_request_timeout_retry(self):
        """request() retries on timeout exceptions."""
        rl = RateLimiter(rate=100.0, burst=10, max_retries=2, base_backoff=0.001)
        mock_fn = AsyncMock(
            side_effect=[httpx.TimeoutException("slow"), {"status": "ok"}]
        )
        result = await rl.request(mock_fn)
        assert result == {"status": "ok"}

    def test_rate_limiter_invalid_rate(self):
        """Constructor rejects non-positive rate."""
        with pytest.raises(ValueError, match="rate must be positive"):
            RateLimiter(rate=0, burst=10)
        with pytest.raises(ValueError, match="rate must be positive"):
            RateLimiter(rate=-1, burst=10)

    def test_rate_limiter_invalid_burst(self):
        """Constructor rejects non-positive burst."""
        with pytest.raises(ValueError, match="burst must be positive"):
            RateLimiter(rate=10, burst=0)
        with pytest.raises(ValueError, match="burst must be positive"):
            RateLimiter(rate=10, burst=-5)

    def test_rate_limiter_backoff_calculation(self):
        """_backoff_for_attempt returns values within expected range."""
        rl = RateLimiter(rate=10.0, burst=10, base_backoff=1.0, max_backoff=30.0)
        b1 = rl._backoff_for_attempt(1)
        assert 0.9 <= b1 <= 1.1  # 1.0 +/- 10% jitter
        b2 = rl._backoff_for_attempt(2)
        assert 1.8 <= b2 <= 2.2  # 2.0 +/- 10% jitter
        b5 = rl._backoff_for_attempt(5)
        assert b5 <= 30.0  # Capped at max_backoff

    def test_rate_limiter_repr(self):
        """__repr__ returns a descriptive string."""
        rl = RateLimiter(rate=10.0, burst=20)
        r = repr(rl)
        assert "RateLimiter" in r


# ---------------------------------------------------------------------------
# TestReconnectionHandler
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    """Tests for ExponentialBackoff -- delay calculation with jitter."""

    def test_backoff_sequence(self):
        """next_delay() produces exponentially growing delays."""
        eb = ExponentialBackoff(base=1.0, max_delay=60.0, jitter=0.0)
        d1 = eb.next_delay()
        assert d1 == 1.0
        d2 = eb.next_delay()
        assert d2 == 2.0
        d3 = eb.next_delay()
        assert d3 == 4.0
        d4 = eb.next_delay()
        assert d4 == 8.0

    def test_backoff_max_cap(self):
        """next_delay() caps at max_delay."""
        eb = ExponentialBackoff(base=1.0, max_delay=10.0, jitter=0.0)
        for _ in range(4):
            eb.next_delay()
        # After 4 attempts: 1, 2, 4, 8
        d5 = eb.next_delay()
        # 5th would be 16 but capped at 10
        assert d5 == 10.0

    def test_backoff_reset(self):
        """reset() clears attempt counter."""
        eb = ExponentialBackoff(base=1.0, max_delay=60.0, jitter=0.0)
        eb.next_delay()  # attempt=1
        eb.next_delay()  # attempt=2
        eb.reset()
        assert eb.attempt == 0
        d = eb.next_delay()
        assert d == 1.0  # Back to base

    def test_backoff_is_at_max(self):
        """is_at_max reports when next delay will hit the cap."""
        eb = ExponentialBackoff(base=1.0, max_delay=8.0, jitter=0.0)
        # Before any delays: next would be 1*2^0 = 1 < 8
        assert eb.is_at_max is False
        eb.next_delay()  # attempt goes 0->1, delay was 1*2^0 = 1
        # Now attempt=1, next would be 1*2^1 = 2 < 8
        assert eb.is_at_max is False
        eb.next_delay()  # attempt 1->2, delay was 2
        # Now attempt=2, next would be 1*2^2 = 4 < 8
        assert eb.is_at_max is False
        eb.next_delay()  # attempt 2->3, delay was 4
        # Now attempt=3, next would be 1*2^3 = 8 == max_delay
        assert eb.is_at_max is True

    def test_backoff_minimum_delay(self):
        """next_delay() respects minimum 100ms floor."""
        eb = ExponentialBackoff(base=0.01, max_delay=60.0, jitter=1.0)
        d = eb.next_delay()
        assert d >= 0.1  # Minimum 100ms

    def test_backoff_with_jitter_range(self):
        """Jitter produces delays within expected range."""
        eb = ExponentialBackoff(base=10.0, max_delay=100.0, jitter=0.1)
        for _ in range(20):
            d = eb.next_delay()
            assert 9.0 <= d <= 11.0  # 10 +/- 10%
            eb.reset()

    def test_backoff_repr(self):
        """ExponentialBackoff has a reasonable repr via dataclass."""
        eb = ExponentialBackoff(base=1.0, max_delay=60.0)
        r = repr(eb)
        assert "ExponentialBackoff" in r


class TestReconnectionHandler:
    """Tests for ReconnectionHandler -- failure tracking and retry decisions."""

    def test_should_reconnect_initially(self):
        """should_reconnect() returns True when no failures recorded."""
        rh = ReconnectionHandler(max_consecutive_failures=5)
        assert rh.should_reconnect() is True

    def test_should_reconnect_after_failures(self):
        """should_reconnect() returns False after max failures reached."""
        rh = ReconnectionHandler(max_consecutive_failures=3)
        rh.record_failure()
        rh.record_failure()
        assert rh.should_reconnect() is True
        rh.record_failure()
        assert rh.should_reconnect() is False

    def test_record_success_resets(self):
        """record_success() resets failure counters."""
        rh = ReconnectionHandler(max_consecutive_failures=5)
        rh.record_failure()
        rh.record_failure()
        assert rh.consecutive_failures == 2
        rh.record_success()
        assert rh.consecutive_failures == 0
        assert rh.is_healthy is True

    def test_record_failure_increments_total(self):
        """record_failure() increments total_reconnections."""
        rh = ReconnectionHandler(max_consecutive_failures=10)
        rh.record_failure()
        rh.record_failure()
        assert rh.total_reconnections == 2

    @pytest.mark.asyncio
    async def test_wait_before_retry_returns_true(self):
        """wait_before_retry() sleeps and returns True when retries remain."""
        rh = ReconnectionHandler(
            max_consecutive_failures=10,
            backoff=MagicMock(),
        )
        rh.backoff.next_delay = MagicMock(return_value=0.001)
        result = await rh.wait_before_retry()
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_before_retry_returns_false_at_max(self):
        """wait_before_retry() returns False when max failures reached."""
        rh = ReconnectionHandler(max_consecutive_failures=2)
        rh.record_failure()
        rh.record_failure()
        result = await rh.wait_before_retry()
        assert result is False

    def test_is_healthy_initially(self):
        """is_healthy is True with zero failures."""
        rh = ReconnectionHandler()
        assert rh.is_healthy is True

    def test_is_healthy_after_failure(self):
        """is_healthy is False after any failure."""
        rh = ReconnectionHandler()
        rh.record_failure()
        assert rh.is_healthy is False

    def test_last_connection_time(self):
        """last_connection_time is set by record_success."""
        rh = ReconnectionHandler()
        assert rh.last_connection_time is None
        rh.record_success()
        assert rh.last_connection_time is not None
        assert isinstance(rh.last_connection_time, float)

    def test_repr(self):
        """__repr__ returns a descriptive string."""
        rh = ReconnectionHandler(max_consecutive_failures=5)
        r = repr(rh)
        assert "ReconnectionHandler" in r
        assert "healthy" in r


# ---------------------------------------------------------------------------
# TestBinanceClient
# ---------------------------------------------------------------------------


class TestBinanceClient:
    """Tests for BinanceClient -- HMAC signing, requests, order placement."""

    def test_client_initialization(self):
        """Client stores auth, testnet flag, base_url."""
        client = BinanceClient(api_key="test_key", api_secret="test_secret", testnet=True)
        assert client._auth.api_key == "test_key"
        assert client._auth._api_secret == "test_secret"
        assert client.testnet is True
        assert "testnet" in client.base_url

    def test_client_initialization_live(self):
        """Client uses live base URL when testnet=False."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=False)
        assert client.testnet is False
        assert client.base_url == "https://api.binance.com"

    def test_client_initialization_requires_credentials(self):
        """Client raises ValueError when credentials are missing."""
        with pytest.raises(ValueError, match="api_key is required"):
            BinanceClient(api_key="", api_secret="secret")
        # api_secret="" is allowed (falls through to Ed25519 check, then fails)
        with pytest.raises(ValueError, match="api_secret.*private_key_pem.*required"):
            BinanceClient(api_key="key", api_secret="")
        # Neither api_secret nor private_key_pem
        with pytest.raises(ValueError, match="api_secret.*private_key_pem.*required"):
            BinanceClient(api_key="key")

    def test_symbol_normalization(self):
        """_norm_symbol converts BTC/USDT -> BTCUSDT."""
        assert BinanceClient._norm_symbol("BTC/USDT") == "BTCUSDT"
        assert BinanceClient._norm_symbol("ETH/BTC") == "ETHBTC"
        assert BinanceClient._norm_symbol("BTCUSDT") == "BTCUSDT"
        assert BinanceClient._norm_symbol("SOL-USDT") == "SOL-USDT"  # No "-" handling

    def test_decimal_helper(self):
        """_d safely converts values to Decimal."""
        assert BinanceClient._d("100.5") == Decimal("100.5")
        assert BinanceClient._d(100) == Decimal("100")
        assert BinanceClient._d(None) == Decimal("0")
        assert BinanceClient._d(Decimal("50")) == Decimal("50")

    def test_hmac_auth_sign(self):
        """BinanceAuth.sign() produces a verifiable HMAC-SHA256 hex signature."""
        from exchange.binance_client import BinanceAuth, AuthMethod

        auth = BinanceAuth.hmac(api_key="k", api_secret="test_secret")
        payload = "symbol=BTCUSDT&side=BUY"
        sig = auth.sign(payload)

        expected = hmac.new(
            b"test_secret",
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected
        assert auth.method == AuthMethod.HMAC_SHA256

    def test_hmac_auth_prepare_request(self):
        """BinanceAuth.prepare_request() injects timestamp, recvWindow, signature."""
        client = BinanceClient(api_key="test_key", api_secret="test_secret", testnet=True)
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        signed_params, headers = client._auth.prepare_request(params)

        assert "timestamp" in signed_params
        assert "recvWindow" in signed_params
        assert "signature" in signed_params
        assert signed_params["symbol"] == "BTCUSDT"
        assert headers["X-MBX-APIKEY"] == "test_key"
        assert isinstance(signed_params["timestamp"], int)
        assert signed_params["recvWindow"] == 5000
        # Signature should be a 64-char hex string (HMAC-SHA256)
        assert len(signed_params["signature"]) == 64

    def test_hmac_signature_verification(self):
        """Signature from prepare_request is independently verifiable."""
        secret = "test_secret"
        auth = BinanceAuth.hmac(api_key="k", api_secret=secret)
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        signed_params, _ = auth.prepare_request(params)

        # Reconstruct the query string (without signature) and verify
        query = urllib.parse.urlencode(
            {k: v for k, v in signed_params.items() if k != "signature"}
        )
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert signed_params["signature"] == expected_sig

    @pytest.mark.asyncio
    async def test_place_market_order(self):
        """place_market_order constructs correct signed POST request."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "symbol": "BTCUSDT",
            "orderId": 12345,
            "status": "FILLED",
            "executedQty": "0.001",
            "cummulativeQuoteQty": "100.00000000",
        }
        client._limiter = MagicMock()
        client._limiter.request = AsyncMock(return_value=mock_response)

        result = await client.place_market_order("BTC/USDT", "BUY", Decimal("0.001"))

        assert result["symbol"] == "BTCUSDT"
        assert result["_executedQtyDecimal"] == Decimal("0.001")
        assert result["_cummulativeQuoteQtyDecimal"] == Decimal("100.00000000")
        client._limiter.request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_balance(self):
        """get_balance returns the free balance for a specific asset."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0.1"},
                {"asset": "USDT", "free": "1000.00", "locked": "100.00"},
                {"asset": "ETH", "free": "2.0", "locked": "0.0"},
            ]
        }
        client._limiter = MagicMock()
        client._limiter.request = AsyncMock(return_value=mock_response)

        balance = await client.get_balance("USDT")
        assert balance == Decimal("1000.00")

    @pytest.mark.asyncio
    async def test_get_balance_missing_asset(self):
        """get_balance returns 0 for an asset not in the account."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}]
        }
        client._limiter = MagicMock()
        client._limiter.request = AsyncMock(return_value=mock_response)

        balance = await client.get_balance("SOL")
        assert balance == Decimal("0")

    @pytest.mark.asyncio
    async def test_get_all_balances(self):
        """get_all_balances returns a dict of non-zero balances."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0.1"},
                {"asset": "USDT", "free": "1000.00", "locked": "100.00"},
                {"asset": "ZERO", "free": "0", "locked": "0"},
            ]
        }
        client._limiter = MagicMock()
        client._limiter.request = AsyncMock(return_value=mock_response)

        balances = await client.get_all_balances()
        assert "BTC" in balances
        assert "USDT" in balances
        assert "ZERO" not in balances
        assert balances["BTC"] == Decimal("0.5")

    @pytest.mark.asyncio
    async def test_get_klines(self):
        """get_klines fetches candlestick data."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        mock_klines = [
            [
                1672531200000,
                "16500.00",
                "16800.00",
                "16450.00",
                "16750.00",
                "12000.00",
                1672617599999,
                "200000000",
                15000,
                "6000.00",
                "100000000",
                "0",
            ]
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_klines
        client._limiter = MagicMock()
        client._limiter.request = AsyncMock(return_value=mock_response)

        result = await client.get_klines("BTC/USDT", "1d", limit=1)
        assert len(result) == 1
        assert result[0][1] == "16500.00"

    @pytest.mark.asyncio
    async def test_close_client(self):
        """close() closes the underlying HTTP client."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        client._client = AsyncMock()
        await client.close()
        client._client.aclose.assert_awaited_once()

    def test_client_repr(self):
        """__repr__ returns a descriptive string."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        r = repr(client)
        assert "BinanceClient" in r

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Client works as an async context manager."""
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        client._client = AsyncMock()
        async with client as c:
            assert c is client
        client._client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestPaperBroker
# ---------------------------------------------------------------------------


class TestPaperBroker:
    """Tests for PaperBroker -- simulated trading with balance tracking.

    NOTE: PaperBroker stores quote assets (e.g. USDT) in ``balances`` and
    base assets (e.g. BTC) in ``positions``.  ``get_balance()`` only looks
    at ``balances``, so base-asset holdings must be checked via
    ``pb.positions[asset]``.
    """

    def test_initial_balance(self):
        """PaperBroker starts with the configured initial balance."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        assert pb.get_balance("USDT") == Decimal("500")
        assert pb.get_balance("BTC") == Decimal("0")

    def test_default_initial_balance(self):
        """PaperBroker default initial balance is 500 USDT."""
        pb = PaperBroker()
        assert pb.get_balance("USDT") == Decimal("500")

    def test_buy_reduces_quote_increases_base(self):
        """BUY reduces quote asset (USDT) and increases base position (BTC)."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        result = pb.place_market_order(
            "BTC/USDT", "BUY", Decimal("0.005"), Decimal("100000")
        )

        cost = Decimal("0.005") * Decimal("100000")  # 500
        assert pb.get_balance("USDT") == Decimal("500") - cost
        assert pb.positions["BTC"] == Decimal("0.005")
        assert result["status"] == "filled"
        assert result["side"] == "BUY"
        assert result["symbol"] == "BTC/USDT"

    def test_sell_realizes_profit(self):
        """SELL at higher price realizes profit correctly."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        # Buy 0.005 BTC at $100k = $500 cost
        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.005"), Decimal("100000"))
        # Sell 0.005 BTC at $105k = $525 proceeds -> $25 profit
        result = pb.place_market_order(
            "BTC/USDT", "SELL", Decimal("0.005"), Decimal("105000")
        )

        assert pb.get_balance("USDT") == Decimal("525")
        assert pb.positions.get("BTC", Decimal("0")) == Decimal("0")
        assert result["status"] == "filled"
        assert result["side"] == "SELL"

    def test_sell_realizes_loss(self):
        """SELL at lower price realizes loss correctly."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        # Buy 0.005 BTC at $100k
        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.005"), Decimal("100000"))
        # Sell 0.005 BTC at $90k = $450 proceeds -> $50 loss
        pb.place_market_order("BTC/USDT", "SELL", Decimal("0.005"), Decimal("90000"))

        assert pb.get_balance("USDT") == Decimal("450")
        assert pb.positions.get("BTC", Decimal("0")) == Decimal("0")

    def test_buy_partial_then_sell_partial(self):
        """Partial buys and sells maintain correct balances."""
        pb = PaperBroker(initial_balance=Decimal("1000"))
        # Buy 0.01 BTC at $100k = $1000 cost -> 0 USDT left
        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.01"), Decimal("100000"))
        assert pb.positions["BTC"] == Decimal("0.01")
        assert pb.get_balance("USDT") == Decimal("0")

        # Sell 0.005 BTC at $110k = $550 proceeds
        pb.place_market_order("BTC/USDT", "SELL", Decimal("0.005"), Decimal("110000"))
        assert pb.positions["BTC"] == Decimal("0.005")
        assert pb.get_balance("USDT") == Decimal("550")

    def test_multiple_symbols(self):
        """Trading different symbols maintains separate positions."""
        pb = PaperBroker(initial_balance=Decimal("2000"))
        # Buy BTC
        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.005"), Decimal("100000"))
        # Buy ETH
        pb.place_market_order("ETH/USDT", "BUY", Decimal("1.0"), Decimal("2000"))

        assert pb.positions["BTC"] == Decimal("0.005")
        assert pb.positions["ETH"] == Decimal("1.0")
        # 2000 - 500 - 2000 = -500 (can go negative -- no guard rails)
        assert pb.get_balance("USDT") == Decimal("-500")

    def test_trade_history_recorded(self):
        """Each order is recorded in trade_history."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        assert len(pb.trade_history) == 0

        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))
        assert len(pb.trade_history) == 1

        pb.place_market_order("BTC/USDT", "SELL", Decimal("0.001"), Decimal("110000"))
        assert len(pb.trade_history) == 2

        trade = pb.trade_history[0]
        assert trade["symbol"] == "BTC/USDT"
        assert trade["side"] == "BUY"
        assert "timestamp" in trade

    def test_trade_history_contains_iso_timestamp(self):
        """Trade records contain valid ISO 8601 timestamps."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        pb.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))

        trade = pb.trade_history[0]
        ts = trade["timestamp"]
        # Should be parseable as ISO 8601
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None  # Has timezone info

    def test_buy_different_symbol_formats(self):
        """Trading pairs with different base assets work correctly."""
        pb = PaperBroker(initial_balance=Decimal("5000"))
        pb.place_market_order("ETH/BTC", "BUY", Decimal("1.0"), Decimal("0.065"))
        # ETH is base -> goes to positions, BTC is quote -> from balances
        assert pb.positions["ETH"] == Decimal("1.0")
        # BTC balance was 0 initially, now negative after spending
        assert pb.get_balance("BTC") == Decimal("-0.065")

    def test_zero_quantity_order(self):
        """Zero quantity order executes without error (no-op)."""
        pb = PaperBroker(initial_balance=Decimal("500"))
        result = pb.place_market_order("BTC/USDT", "BUY", Decimal("0"), Decimal("100000"))
        assert result["status"] == "filled"
        assert pb.get_balance("USDT") == Decimal("500")  # Unchanged
        assert pb.positions.get("BTC", Decimal("0")) == Decimal("0")

    def test_negative_balance_allowed(self):
        """PaperBroker allows negative balances (no guard rails)."""
        pb = PaperBroker(initial_balance=Decimal("100"))
        # Overspend: buy $500 worth with only $100
        result = pb.place_market_order(
            "BTC/USDT", "BUY", Decimal("0.005"), Decimal("100000")
        )
        assert result["status"] == "filled"
        assert pb.get_balance("USDT") == Decimal("-400")  # 100 - 500
        assert pb.positions["BTC"] == Decimal("0.005")

    def test_normalize_symbol_helper(self):
        """_normalize_symbol converts human-readable to Binance stream format."""
        assert _normalize_symbol("BTC/USDT") == "btcusdt"
        assert _normalize_symbol("ETH/BTC") == "ethbtc"
        assert _normalize_symbol("BTCUSDT") == "btcusdt"
        assert _normalize_symbol("SOL-USDT") == "solusdt"

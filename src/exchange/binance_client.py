"""
Crypto Trading Bot - Binance Client
====================================

Production-grade async Binance API client.

Features
--------
- HMAC-SHA256 request signing
- Automatic ``timestamp`` + ``recvWindow`` injection
- Token-bucket rate limiting on every call
- Proper HTTP error handling (429, 418, 4xx, 5xx)
- Decimal precision for all monetary values
- Symbol normalisation (``BTC/USDT`` → ``BTCUSDT``)

Environment
-----------
Set ``BINANCE_API_KEY`` and ``BINANCE_API_SECRET`` env vars, or pass
them directly to the constructor.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TESTNET_BASE: str = "https://testnet.binance.vision"
_LIVE_BASE: str = "https://api.binance.com"
_RECV_WINDOW: int = 5000  # ms
_DEFAULT_TIMEOUT: float = 30.0


class BinanceClient:
    """Async Binance REST API client.

    Parameters
    ----------
    api_key:
        Binance API key.
    api_secret:
        Binance API secret used for HMAC-SHA256 signing.
    testnet:
        If ``True`` (default), connect to the Binance testnet.
    rate_limiter:
        Optional :class:`RateLimiter` instance.  A default is created if none
        is supplied.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")

        self.api_key: str = api_key
        self.api_secret: str = api_secret
        self.testnet: bool = testnet
        self.base_url: str = _TESTNET_BASE if testnet else _LIVE_BASE

        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_DEFAULT_TIMEOUT,
            headers={"X-MBX-APIKEY": self.api_key},
        )

        self._limiter: RateLimiter = rate_limiter or RateLimiter(
            rate=10.0, burst=20
        )

        logger.info(
            "BinanceClient initialised — testnet=%s base=%s",
            self.testnet,
            self.base_url,
        )

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"BinanceClient(testnet={self.testnet}, base_url={self.base_url}, "
            f"limiter={self._limiter!r})"
        )

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        """Normalise symbol: ``BTC/USDT`` → ``BTCUSDT``."""
        return symbol.replace("/", "")

    @staticmethod
    def _d(val: Any) -> Decimal:
        """Safely convert a value to Decimal."""
        if val is None:
            return Decimal("0")
        if isinstance(val, Decimal):
            return val
        return Decimal(str(val))

    # ------------------------------------------------------------------ #
    # Request signing
    # ------------------------------------------------------------------ #

    def _sign(self, params: Dict[str, Any]) -> str:
        """Sign *params* with HMAC-SHA256 and return the full query string.

        Mutates *params* in-place by adding ``timestamp`` and ``recvWindow``.
        """
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = _RECV_WINDOW
        query = urllib.parse.urlencode(params)
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={sig}"

    # ------------------------------------------------------------------ #
    # Low-level HTTP
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        """Unsigned GET request."""
        params = params or {}
        response = await self._limiter.request(
            self._client.get,
            path,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def _get_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed GET request (for account endpoints)."""
        params = params or {}
        query_string = self._sign(params)
        response = await self._limiter.request(
            self._client.get,
            f"{path}?{query_string}",
        )
        response.raise_for_status()
        return response.json()

    async def _post_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed POST request (for order placement)."""
        params = params or {}
        query_string = self._sign(params)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = await self._limiter.request(
            self._client.post,
            f"{path}?{query_string}",
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def _delete_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed DELETE request (for order cancellation)."""
        params = params or {}
        query_string = self._sign(params)
        response = await self._limiter.request(
            self._client.delete,
            f"{path}?{query_string}",
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------ #
    # Public API methods
    # ------------------------------------------------------------------ #

    # -- Market data ----------------------------------------------------- #

    async def get_exchange_info(self) -> dict:
        """Fetch exchange info (symbols, filters, etc.)."""
        logger.debug("Fetching exchange info")
        return await self._get("/api/v3/exchangeInfo")

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> List[List]:
        """Fetch kline (candlestick) data.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        interval:
            Kline interval, e.g. ``"1h"``, ``"15m"``, ``"1d"``.
        limit:
            Number of candles to fetch (default 500, max 1000).
        """
        norm = self._norm_symbol(symbol)
        logger.debug("Fetching klines for %s %s limit=%d", norm, interval, limit)
        params = {"symbol": norm, "interval": interval, "limit": limit}
        return await self._get("/api/v3/klines", params)

    # -- Account --------------------------------------------------------- #

    async def get_account_info(self) -> dict:
        """Fetch full account information including balances."""
        logger.debug("Fetching account info")
        return await self._get_signed("/api/v3/account")

    async def get_all_balances(self) -> Dict[str, Decimal]:
        """Return a mapping ``{asset: free_balance}`` for all non-zero balances."""
        info = await self.get_account_info()
        balances: Dict[str, Decimal] = {}
        for bal in info.get("balances", []):
            asset = bal["asset"]
            free = self._d(bal.get("free", "0"))
            locked = self._d(bal.get("locked", "0"))
            if free > 0 or locked > 0:
                balances[asset] = free
        logger.debug("Retrieved %d non-zero balances", len(balances))
        return balances

    async def get_balance(self, asset: str) -> Decimal:
        """Return the free balance for a single *asset* (e.g. ``"USDT"``)."""
        info = await self.get_account_info()
        for bal in info.get("balances", []):
            if bal["asset"] == asset:
                return self._d(bal.get("free", "0"))
        return Decimal("0")

    # -- Orders ---------------------------------------------------------- #

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
    ) -> dict:
        """Place a market order.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        side:
            ``"BUY"`` or ``"SELL"``.
        quantity:
            Amount of the *base* asset to buy/sell.

        Returns
        -------
        dict
            Raw Binance response with all fields as strings; ``executedQty``
            and ``cummulativeQuoteQty`` are additionally parsed into
            ``executedQtyDecimal`` / ``cummulativeQuoteQtyDecimal``.
        """
        norm = self._norm_symbol(symbol)
        logger.info(
            "Placing market order: %s %s %s",
            side,
            quantity,
            symbol,
        )

        params: Dict[str, Any] = {
            "symbol": norm,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
        }

        result = await self._post_signed("/api/v3/order", params)

        # Enrich response with Decimal versions
        result["_executedQtyDecimal"] = self._d(result.get("executedQty", "0"))
        result["_cummulativeQuoteQtyDecimal"] = self._d(
            result.get("cummulativeQuoteQty", "0")
        )

        logger.info(
            "Market order filled — symbol=%s side=%s executedQty=%s quoteQty=%s",
            result.get("symbol"),
            result.get("side"),
            result.get("executedQty"),
            result.get("cummulativeQuoteQty"),
        )
        return result

    async def get_order(self, symbol: str, order_id: int) -> dict:
        """Query a single order by *order_id*."""
        norm = self._norm_symbol(symbol)
        logger.debug("Querying order %s for %s", order_id, symbol)
        params: Dict[str, Any] = {"symbol": norm, "orderId": order_id}
        return await self._get_signed("/api/v3/order", params)

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an open order by *order_id*."""
        norm = self._norm_symbol(symbol)
        logger.info("Cancelling order %s for %s", order_id, symbol)
        params: Dict[str, Any] = {"symbol": norm, "orderId": order_id}
        return await self._delete_signed("/api/v3/order", params)

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        logger.debug("Closing BinanceClient HTTP session")
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Async context-manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "BinanceClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

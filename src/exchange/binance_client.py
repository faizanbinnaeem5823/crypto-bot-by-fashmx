"""
Crypto Trading Bot - Binance Client
====================================

Production-grade async Binance API client.

Features
--------
- Dual authentication: HMAC-SHA256 (legacy) **and** Ed25519 (enhanced)
- Automatic ``timestamp`` + ``recvWindow`` injection
- Token-bucket rate limiting on every call
- Proper HTTP error handling (429, 418, 4xx, 5xx)
- Decimal precision for all monetary values
- Symbol normalisation (``BTC/USDT`` → ``BTCUSDT``)

Environment
-----------
HMAC mode — set ``BINANCE_API_KEY`` and ``BINANCE_API_SECRET``.

Ed25519 mode — set ``BINANCE_API_KEY`` and ``BINANCE_PRIVATE_KEY_PATH``.

Usage
-----
>>> # HMAC (legacy)
>>> client = BinanceClient(api_key="...", api_secret="...")

>>> # Ed25519 (enhanced security)
>>> client = BinanceClient(api_key="...", private_key_pem="-----BEGIN PRIVATE KEY-----...")
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TESTNET_BASE: str = "https://testnet.binance.vision"
_LIVE_BASE: str = "https://api.binance.com"
_RECV_WINDOW: int = 5000  # ms
_DEFAULT_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class AuthMethod(Enum):
    """Supported Binance API authentication methods."""

    HMAC_SHA256 = "hmac"
    ED25519 = "ed25519"


class BinanceAuth:
    """Binance API authentication — supports HMAC-SHA256 and Ed25519.

    Usage (HMAC)::::

        auth = BinanceAuth.hmac(api_key="key", api_secret="secret")

    Usage (Ed25519)::::

        auth = BinanceAuth.ed25519(api_key="key", private_key_pem="pem_string")
    """

    # ------------------------------------------------------------------ #
    # Factory constructors
    # ------------------------------------------------------------------ #

    def __init__(self, api_key: str, method: AuthMethod) -> None:
        self.api_key: str = api_key
        self.method: AuthMethod = method

    @classmethod
    def hmac(cls, api_key: str, api_secret: str) -> "BinanceAuth":
        """Create an HMAC-SHA256 authenticator."""
        auth = cls(api_key, AuthMethod.HMAC_SHA256)
        auth._api_secret: str = api_secret
        return auth

    @classmethod
    def ed25519(cls, api_key: str, private_key_pem: str) -> "BinanceAuth":
        """Create an Ed25519 authenticator from a PEM-encoded private key."""
        auth = cls(api_key, AuthMethod.ED25519)
        auth._private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        return auth

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #

    def sign(self, payload: str) -> str:
        """Sign *payload* using the configured method.

        Returns
        -------
        str
            HMAC: hex-encoded signature.
            Ed25519: base64-encoded signature.
        """
        if self.method == AuthMethod.HMAC_SHA256:
            return hmac.new(
                self._api_secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        elif self.method == AuthMethod.ED25519:
            signature: bytes = self._private_key.sign(payload.encode("utf-8"))
            return base64.b64encode(signature).decode("ascii")
        else:
            raise ValueError(f"Unknown auth method: {self.method}")

    # ------------------------------------------------------------------ #
    # Request preparation
    # ------------------------------------------------------------------ #

    def prepare_request(
        self, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Prepare signed request parameters and headers.

        Injects ``timestamp`` and ``recvWindow`` into *params* (mutates in
        place) and returns the updated params dict plus the HTTP headers that
        must be sent with the request.

        Parameters
        ----------
        params:
            Query parameters for the request.

        Returns
        -------
        Tuple[dict, dict]
            ``(params, headers)`` — ready to pass to *httpx*.
        """
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = _RECV_WINDOW

        query = urllib.parse.urlencode(params)

        headers: Dict[str, str] = {"X-MBX-APIKEY": self.api_key}

        if self.method == AuthMethod.HMAC_SHA256:
            # HMAC: signature is appended as a query parameter
            params["signature"] = self.sign(query)
        elif self.method == AuthMethod.ED25519:
            # Ed25519: signature is sent in the X-MBX-SIGNATURE header
            headers["X-MBX-SIGNATURE"] = self.sign(query)

        return params, headers

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return f"BinanceAuth(method={self.method.value})"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BinanceClient:
    """Async Binance REST API client.

    Parameters
    ----------
    api_key:
        Binance API key.
    api_secret:
        API secret used for HMAC-SHA256 signing (legacy).
    private_key_pem:
        Ed25519 private key in PEM format (enhanced security).
    testnet:
        If ``True`` (default), connect to the Binance testnet.
    rate_limiter:
        Optional :class:`RateLimiter` instance.  A default is created if none
        is supplied.

    Raises
    ------
    ValueError
        If neither *api_secret* nor *private_key_pem* is provided.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        api_key: str,
        api_secret: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        testnet: bool = True,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")

        # -- Authentication ------------------------------------------------
        if private_key_pem:
            self._auth: BinanceAuth = BinanceAuth.ed25519(api_key, private_key_pem)
            logger.info("BinanceClient auth=Ed25519 (enhanced)")
        elif api_secret:
            self._auth: BinanceAuth = BinanceAuth.hmac(api_key, api_secret)
            logger.info("BinanceClient auth=HMAC-SHA256 (legacy)")
        else:
            raise ValueError(
                "Either api_secret (HMAC) or private_key_pem (Ed25519) is required"
            )

        # -- HTTP client ---------------------------------------------------
        self.testnet: bool = testnet
        self.base_url: str = _TESTNET_BASE if testnet else _LIVE_BASE

        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_DEFAULT_TIMEOUT,
        )

        self._limiter: RateLimiter = rate_limiter or RateLimiter(
            rate=10.0, burst=20
        )

        logger.info(
            "BinanceClient initialised — testnet=%s base=%s auth=%s",
            self.testnet,
            self.base_url,
            self._auth,
        )

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"BinanceClient(testnet={self.testnet}, base_url={self.base_url}, "
            f"auth={self._auth!r}, limiter={self._limiter!r})"
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
    # Low-level HTTP (unsigned)
    # ------------------------------------------------------------------ #

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Unsigned GET request."""
        params = params or {}
        response = await self._limiter.request(
            self._client.get,
            path,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------ #
    # Low-level HTTP (signed)
    # ------------------------------------------------------------------ #

    async def _get_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed GET request (for account endpoints)."""
        params = params or {}
        signed_params, headers = self._auth.prepare_request(params)
        response = await self._limiter.request(
            self._client.get,
            path,
            params=signed_params,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def _post_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed POST request (for order placement)."""
        params = params or {}
        signed_params, headers = self._auth.prepare_request(params)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = await self._limiter.request(
            self._client.post,
            path,
            params=signed_params,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def _delete_signed(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Signed DELETE request (for order cancellation)."""
        params = params or {}
        signed_params, headers = self._auth.prepare_request(params)
        response = await self._limiter.request(
            self._client.delete,
            path,
            params=signed_params,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------ #
    # Ed25519 key generation
    # ------------------------------------------------------------------ #

    @staticmethod
    def generate_ed25519_keypair() -> Tuple[str, str]:
        """Generate a new Ed25519 keypair for Binance API.

        Returns
        -------
        Tuple[str, str]
            ``(private_key_pem, public_key_pem)``

        Notes
        -----
        * Give the **public** key to Binance when creating an API key.
        * Store the **private** key securely — never commit it to git.

        Example
        -------
        >>> priv, pub = BinanceClient.generate_ed25519_keypair()
        """
        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        public_key = private_key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        return private_pem, public_pem

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

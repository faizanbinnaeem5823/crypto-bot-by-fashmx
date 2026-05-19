"""CryptoPanic API client (free tier).

Free tier: 1 request per 5 seconds, basic sentiment data.
NEVER use for trading signals — halt-only.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

CRYPTOPANIC_BASE = "https://cryptopanic.com/api/v1"


class CryptoPanicClient:
    """CryptoPanic news client (free tier).

    Usage:
        client = CryptoPanicClient(api_key="your_key")
        posts = await client.get_posts(currencies="BTC,ETH", limit=10)
        for post in posts:
            print(post["title"], post["sentiment"])
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=10.0)

    async def get_posts(self, currencies: str = "BTC,ETH", filter_type: str = "important",
                        limit: int = 10) -> List[Dict]:
        """Fetch recent posts for given currencies."""
        params = {
            "auth_token": self.api_key,
            "currencies": currencies,
            "filter": filter_type,
            "limit": limit,
        }
        try:
            resp = await self.client.get(f"{CRYPTOPANIC_BASE}/posts/", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"CryptoPanic API error: {e}")
            return []

    async def close(self):
        await self.client.aclose()

"""RSS feed fetcher for crypto news sources."""

import logging
from datetime import datetime, timezone
from typing import Dict, List

import feedparser
import httpx

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
}


class RSSFetcher:
    """Fetch and parse RSS feeds from crypto news sources.

    Usage:
        fetcher = RSSFetcher()
        articles = await fetcher.fetch_all(limit=20)
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)

    async def fetch_all(self, limit: int = 20) -> List[Dict]:
        """Fetch articles from all configured RSS feeds."""
        all_articles = []
        for source, url in RSS_FEEDS.items():
            try:
                articles = await self._fetch_feed(source, url, limit)
                all_articles.extend(articles)
            except Exception as e:
                logger.error(f"RSS fetch error for {source}: {e}")

        # Sort by date, most recent first
        all_articles.sort(key=lambda x: x.get("published", ""), reverse=True)
        return all_articles[:limit]

    async def _fetch_feed(self, source: str, url: str, limit: int) -> List[Dict]:
        """Fetch a single RSS feed."""
        resp = await self.client.get(url)
        resp.raise_for_status()

        feed = feedparser.parse(resp.text)
        articles = []
        for entry in feed.entries[:limit]:
            articles.append({
                "source": source,
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", "")[:500],
            })
        return articles

    async def close(self):
        await self.client.aclose()

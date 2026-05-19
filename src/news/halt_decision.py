"""Halt decision engine: determines if news sentiment is bad enough to pause trading.

IRON RULE: This module is NEVER in the trade-decision path. It can only HALT trading.
It cannot generate buy/sell signals.
"""

import logging
from typing import Dict, List

from .sentiment_filter import SentimentFilter

logger = logging.getLogger(__name__)


class HaltDecision:
    """Decide whether to halt trading based on news sentiment.

    Returns HALT if:
    - > 50% of recent articles are extremely negative
    - Keywords indicating major events (hack, ban, exchange failure)
    - Sentiment score below threshold

    NEVER returns BUY or SELL.
    """

    def __init__(self, halt_threshold: float = -0.8, min_articles: int = 5):
        self.filter = SentimentFilter(halt_threshold=halt_threshold)
        self.min_articles = min_articles
        self.halt_keywords = [
            "hack", "exploit", "security breach",
            "ban", "regulation crackdown", "illegal",
            "exchange failure", "insolvent", "bankruptcy",
            "ponzi", "scam", "fraud",
        ]

    def should_halt(self, articles: List[Dict]) -> tuple[bool, str]:
        """Check if trading should be halted.

        Returns (should_halt, reason).
        """
        if len(articles) < self.min_articles:
            return False, "insufficient_articles"

        # Check for halt keywords
        halt_count = 0
        for article in articles:
            title_lower = article.get("title", "").lower()
            if any(kw in title_lower for kw in self.halt_keywords):
                halt_count += 1

        if halt_count >= 2:
            return True, f"halt_keywords_detected:{halt_count}_articles"

        # Check sentiment
        negative_count = 0
        for article in articles[:self.min_articles]:
            sentiment = self.filter.classify(article.get("title", ""))
            if sentiment == "negative":
                negative_count += 1

        if negative_count / self.min_articles > 0.5:
            return True, f"negative_sentiment:{negative_count}/{self.min_articles}"

        return False, "sentiment_ok"

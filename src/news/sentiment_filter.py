"""Sentiment filter for news headlines using keyword-based analysis.

Used exclusively by the halt decision engine — NEVER for trading signals.
"""

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

# Word lists for simple keyword-based sentiment scoring
POSITIVE_WORDS = [
    "bull", "bullish", "surge", "rally", "adoption",
    "partnership", "launch", "upgrade", "gain", "soar",
    " ATH", "all-time high", "moon", "breakthrough",
    "approval", "ETF", "institutional",
]

NEGATIVE_WORDS = [
    "bear", "bearish", "crash", "plunge", "dump",
    "hack", "exploit", "breach", "scam", "fraud",
    "ban", "illegal", "crackdown", "regulation",
    "insolvent", "bankruptcy", "liquidation", "collapse",
    "ponzi", "lawsuit", "SEC", "investigation",
    "sell-off", "panic", "fear", "recession",
]

NEUTRAL_WORDS = [
    "update", "report", "analysis", "review",
    "interview", "podcast", "guide", "explained",
]


class SentimentFilter:
    """Keyword-based sentiment classifier for news headlines.

    IRON RULE: This class is ONLY used for halt decisions.
    It does NOT generate buy/sell signals.
    """

    def __init__(self, halt_threshold: float = -0.8):
        self.halt_threshold = halt_threshold

    def classify(self, text: str) -> str:
        """Classify text sentiment.

        Returns: "positive", "negative", or "neutral"
        """
        text_lower = text.lower()
        pos_count = sum(1 for w in POSITIVE_WORDS if w.lower() in text_lower)
        neg_count = sum(1 for w in NEGATIVE_WORDS if w.lower() in text_lower)
        neu_count = sum(1 for w in NEUTRAL_WORDS if w.lower() in text_lower)

        if neg_count > pos_count and neg_count > neu_count:
            return "negative"
        elif pos_count > neg_count and pos_count > neu_count:
            return "positive"
        else:
            return "neutral"

    def score(self, text: str) -> float:
        """Compute a sentiment score in range [-1, 1]."""
        text_lower = text.lower()
        pos_count = sum(1 for w in POSITIVE_WORDS if w.lower() in text_lower)
        neg_count = sum(1 for w in NEGATIVE_WORDS if w.lower() in text_lower)
        total = pos_count + neg_count
        if total == 0:
            return 0.0
        return (pos_count - neg_count) / total

import logging
from datetime import datetime, timezone
from typing import Dict

logger = logging.getLogger(__name__)

class SentimentFilter:
    """News sentiment filter - halt-only (no trading signals from news)."""

    def __init__(self, halt_threshold: float = -0.8):
        self.halt_threshold = halt_threshold

    def classify(self, text: str) -> str:
        positive_words = ['rally', 'bull', 'breakout', 'surge', 'moon', 'adoption']
        negative_words = ['crash', 'hack', 'ban', 'fraud', 'collapse', 'crisis', 'scam']
        text_lower = text.lower()
        pos = sum(1 for w in positive_words if w in text_lower)
        neg = sum(1 for w in negative_words if w in text_lower)
        if neg > pos:
            return "negative"
        elif pos > neg:
            return "positive"
        return "neutral"

    def should_halt_trading(self, text: str) -> bool:
        sentiment = self.classify(text)
        score = -1.0 if sentiment == "negative" else 0.0 if sentiment == "neutral" else 1.0
        return score <= self.halt_threshold

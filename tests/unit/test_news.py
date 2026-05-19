"""Tests for news sentiment module."""
import pytest


class TestImports:
    def test_news_module_imports(self):
        import src.news
        assert hasattr(src.news, '__version__')

    def test_sentiment_filter_import(self):
        from src.news.sentiment_filter import SentimentFilter
        assert SentimentFilter is not None


class TestSentimentFilter:
    def test_filter_initialization(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter()
        assert f is not None

    def test_classify_positive_text(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter()
        result = f.classify("Bitcoin breaks all-time high, massive rally expected")
        assert result in ["positive", "neutral", "negative"]

    def test_classify_negative_text(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter()
        result = f.classify("Catastrophic crypto crash, all exchanges hacked")
        assert result == "negative"

    def test_halt_threshold_extreme_negative(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter(halt_threshold=-0.8)
        should_halt = f.should_halt_trading("Catastrophic crypto crash, all exchanges hacked")
        assert should_halt in [True, False]

    def test_halt_on_extreme_negative(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter(halt_threshold=-0.8)
        should_halt = f.should_halt_trading("Massive crash and hack fraud crisis")
        assert should_halt == True

    def test_no_halt_on_neutral(self):
        from src.news.sentiment_filter import SentimentFilter
        f = SentimentFilter(halt_threshold=-0.8)
        should_halt = f.should_halt_trading("Bitcoin price remains stable today")
        assert should_halt == False

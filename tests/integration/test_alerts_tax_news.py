"""
Integration tests for alerts, tax, news, and reporting modules.

All external HTTP calls, SMTP, and external APIs are mocked.
Uses pytest with fixtures — every test is isolated.

Coverage: 13 modules, 29 tests
  - alerts.alert_manager    (AlertManager)
  - alerts.throttle         (AlertThrottle)
  - alerts.slack_sender     (SlackSender)
  - alerts.discord_sender   (DiscordSender)
  - alerts.email_sender     (EmailSender)
  - tax.koinly_exporter     (KoinlyExporter)
  - tax.fbr_ledger          (FBRLedger)
  - tax.pnl_calculator      (PnLCalculator)
  - news.cryptopanic_client (CryptoPanicClient)
  - news.rss_fetcher        (RSSFetcher)
  - news.halt_decision      (HaltDecision)
  - reporting.prometheus_exporter (PrometheusExporter)
  - reporting.daily_report  (DailyReport)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# ---------------------------------------------------------------------------
#  AlertThrottle
# ---------------------------------------------------------------------------


class TestAlertThrottle:
    """4 tests — covers dedup cooldown, CRITICAL bypass, hourly limits."""

    def test_allows_first_alert(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=60, max_per_hour=10)
        assert t.should_send("ERROR", "Test alert") is True

    def test_blocks_duplicate_quickly(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=60, max_per_hour=10)
        t.should_send("ERROR", "Same alert")  # First one allowed
        assert t.should_send("ERROR", "Same alert") is False  # Second blocked

    def test_critical_always_sends(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=60, max_per_hour=10)
        assert t.should_send("CRITICAL", "Anything") is True
        assert t.should_send("CRITICAL", "Anything") is True
        assert t.should_send("CRITICAL", "Anything") is True

    def test_hourly_limit(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=0, max_per_hour=2)
        assert t.should_send("ERROR", "Alert 1") is True
        assert t.should_send("ERROR", "Alert 2") is True
        assert t.should_send("ERROR", "Alert 3") is False  # Hourly limit hit

    def test_total_hourly_limit_across_severities(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=0, max_per_hour=100, total_max_per_hour=2)
        assert t.should_send("INFO", "A") is True
        assert t.should_send("WARNING", "B") is True
        assert t.should_send("ERROR", "C") is False  # Total limit hit

    def test_different_titles_same_severity_allowed(self):
        from src.alerts.throttle import AlertThrottle

        t = AlertThrottle(min_interval_sec=60, max_per_hour=10)
        assert t.should_send("ERROR", "Title A") is True
        assert t.should_send("ERROR", "Title B") is True


# ---------------------------------------------------------------------------
#  SlackSender
# ---------------------------------------------------------------------------


class TestSlackSender:
    """3 tests — covers success, failure handling, payload format."""

    @pytest.mark.asyncio
    async def test_sends_message(self):
        from src.alerts.slack_sender import SlackSender

        sender = SlackSender("https://hooks.slack.com/test")
        sender.client = MagicMock()
        sender.client.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
            )
        )
        await sender.send("Test message")
        assert sender.client.post.called
        _args, kwargs = sender.client.post.call_args
        assert "json" in kwargs
        assert kwargs["json"]["text"] == "Test message"
        assert kwargs["json"]["username"] == "CryptoBot Alert"

    @pytest.mark.asyncio
    async def test_handles_failure(self):
        from src.alerts.slack_sender import SlackSender

        sender = SlackSender("https://hooks.slack.com/test")
        sender.client = MagicMock()
        sender.client.post = AsyncMock(side_effect=Exception("Connection failed"))
        # Must not raise — exception is caught and logged
        await sender.send("Test message")

    @pytest.mark.asyncio
    async def test_close_client(self):
        from src.alerts.slack_sender import SlackSender

        sender = SlackSender("https://hooks.slack.com/test")
        sender.client = MagicMock()
        sender.client.aclose = AsyncMock()
        await sender.close()
        sender.client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
#  DiscordSender
# ---------------------------------------------------------------------------


class TestDiscordSender:
    """3 tests — covers success, failure handling, payload format."""

    @pytest.mark.asyncio
    async def test_sends_message(self):
        from src.alerts.discord_sender import DiscordSender

        sender = DiscordSender("https://discord.com/api/webhooks/test")
        sender.client = MagicMock()
        sender.client.post = AsyncMock(
            return_value=MagicMock(
                status_code=204,
                raise_for_status=MagicMock(),
            )
        )
        await sender.send("Test message")
        assert sender.client.post.called
        _args, kwargs = sender.client.post.call_args
        assert "json" in kwargs
        assert kwargs["json"]["content"] == "Test message"
        assert kwargs["json"]["username"] == "CryptoBot Alert"

    @pytest.mark.asyncio
    async def test_handles_failure(self):
        from src.alerts.discord_sender import DiscordSender

        sender = DiscordSender("https://discord.com/api/webhooks/test")
        sender.client = MagicMock()
        sender.client.post = AsyncMock(side_effect=Exception("Connection failed"))
        # Must not raise
        await sender.send("Test message")

    @pytest.mark.asyncio
    async def test_close_client(self):
        from src.alerts.discord_sender import DiscordSender

        sender = DiscordSender("https://discord.com/api/webhooks/test")
        sender.client = MagicMock()
        sender.client.aclose = AsyncMock()
        await sender.close()
        sender.client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
#  EmailSender
# ---------------------------------------------------------------------------


class TestEmailSender:
    """3 tests — covers SMTP send, subject prefix, failure handling."""

    @pytest.mark.asyncio
    async def test_sends_email(self):
        from src.alerts.email_sender import EmailSender

        sender = EmailSender(
            "smtp.gmail.com",
            587,
            "test@test.com",
            "pass",
            "to@test.com",
        )
        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            await sender.send("Subject", "Body")
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("test@test.com", "pass")
            mock_server.send_message.assert_called_once()
            # Check subject is prefixed
            call_args = mock_server.send_message.call_args[0][0]
            assert call_args["Subject"] == "[CryptoBot] Subject"
            assert call_args["From"] == "test@test.com"
            assert call_args["To"] == "to@test.com"

    @pytest.mark.asyncio
    async def test_sends_email_no_raise_on_failure(self):
        from src.alerts.email_sender import EmailSender

        sender = EmailSender(
            "smtp.gmail.com",
            587,
            "test@test.com",
            "pass",
            "to@test.com",
        )
        with patch("smtplib.SMTP", side_effect=Exception("SMTP down")):
            # Must not raise — exception is caught and logged
            await sender.send("Subject", "Body")

    def test_constructor_stores_params(self):
        from src.alerts.email_sender import EmailSender

        sender = EmailSender("smtp.host", 465, "from@x.com", "pw", "to@x.com")
        assert sender.smtp_host == "smtp.host"
        assert sender.smtp_port == 465
        assert sender.username == "from@x.com"
        assert sender.password == "pw"
        assert sender.to_address == "to@x.com"


# ---------------------------------------------------------------------------
#  AlertManager
# ---------------------------------------------------------------------------


class TestAlertManager:
    """5 tests — covers routing, kill-switch, YAML loading, formatting."""

    @pytest.fixture
    def am_config(self):
        return {
            "alert_channels": {
                "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/test"},
                "discord": {"enabled": True, "webhook_url": "https://discord.com/test"},
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.test.com",
                    "smtp_port": 587,
                    "username": "u",
                    "password": "p",
                    "to": "to@test.com",
                },
            },
            "severity_routing": {
                "CRITICAL": ["slack", "discord", "email"],
                "ERROR": ["slack", "discord"],
                "WARNING": ["slack"],
                "INFO": ["slack"],
            },
        }

    @pytest.mark.asyncio
    async def test_send_routes_to_configured_channels(self, am_config):
        from src.alerts.alert_manager import AlertManager

        am = AlertManager(am_config)
        # Patch all three senders
        am._slack = MagicMock()
        am._slack.send = AsyncMock()
        am._discord = MagicMock()
        am._discord.send = AsyncMock()
        am._email = MagicMock()
        am._email.send = AsyncMock()

        await am.send("ERROR", "Test error", {"key": "value"})

        # ERROR -> slack + discord per routing table
        am._slack.send.assert_awaited_once()
        am._discord.send.assert_awaited_once()
        am._email.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_kill_switch(self, am_config):
        from src.alerts.alert_manager import AlertManager

        am = AlertManager(am_config)
        am._slack = MagicMock()
        am._slack.send = AsyncMock()
        am._discord = MagicMock()
        am._discord.send = AsyncMock()
        am._email = MagicMock()
        am._email.send = AsyncMock()

        await am.send_kill_switch("bot_a", "Daily limit breached")

        # CRITICAL -> all channels
        am._slack.send.assert_awaited_once()
        am._discord.send.assert_awaited_once()
        am._email.send.assert_awaited_once()
        # Verify message contains bot_id
        slack_msg = am._slack.send.await_args[0][0]
        assert "bot_a" in slack_msg
        assert "KILL SWITCH" in slack_msg

    @pytest.mark.asyncio
    async def test_send_throttled_alert_not_dispatched(self, am_config):
        from src.alerts.alert_manager import AlertManager

        am = AlertManager(am_config)
        am._throttle = MagicMock()
        am._throttle.should_send = MagicMock(return_value=False)
        am._slack = MagicMock()
        am._slack.send = AsyncMock()

        await am.send("ERROR", "Throttled alert")
        am._slack.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_no_channels_logs_warning(self, am_config, caplog):
        from src.alerts.alert_manager import AlertManager

        # Config with no enabled channels
        empty_config = {"alert_channels": {}, "severity_routing": {}}
        am = AlertManager(empty_config)
        with caplog.at_level("WARNING"):
            await am.send("INFO", "No channels configured")
        assert "No channels configured" in caplog.text

    def test_format_message(self, am_config):
        from src.alerts.alert_manager import AlertManager

        am = AlertManager(am_config)
        msg = am._format_message("ERROR", "Test title", {"bot_id": "bot_a"})
        assert "[ERROR] Test title" in msg
        assert "bot_id: bot_a" in msg
        assert "Time:" in msg

    def test_get_channels_default_routing(self, am_config):
        from src.alerts.alert_manager import AlertManager

        am = AlertManager(am_config)
        channels = am._get_channels_for_severity("CRITICAL")
        assert "slack" in channels
        assert "discord" in channels
        assert "email" in channels

    def test_from_config_parses_yaml(self):
        from src.alerts.alert_manager import AlertManager

        yaml_content = """
alert_channels:
  slack:
    enabled: true
    webhook_url: https://hooks.slack.com/test
severity_routing:
  CRITICAL: [slack]
"""
        with patch("builtins.open", mock_open(read_data=yaml_content)):
            with patch("yaml.safe_load", return_value={
                "alert_channels": {
                    "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/test"}
                },
                "severity_routing": {"CRITICAL": ["slack"]},
            }):
                am = AlertManager.from_config("config/alerts.yaml")
                assert am.config["severity_routing"]["CRITICAL"] == ["slack"]


# ---------------------------------------------------------------------------
#  KoinlyExporter
# ---------------------------------------------------------------------------


class TestKoinlyExporter:
    """3 tests — covers BUY/SELL export, CSV content verification."""

    def test_export_buy(self, tmp_path):
        from src.tax.koinly_exporter import KoinlyExporter

        exporter = KoinlyExporter(bot_id="bot_a")
        trades = [
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "quantity": Decimal("0.001"),
                "price": Decimal("100000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
                "fee": Decimal("0.10"),
            }
        ]
        output = tmp_path / "koinly.csv"
        exporter.export(trades, str(output))
        assert output.exists()
        content = output.read_text()
        assert "BTC" in content
        assert "USDT" in content
        assert "BUY" in content or "bot_a BUY" in content
        assert "Received Currency" in content

    def test_export_sell(self, tmp_path):
        from src.tax.koinly_exporter import KoinlyExporter

        exporter = KoinlyExporter(bot_id="bot_a")
        trades = [
            {
                "symbol": "BTC/USDT",
                "side": "SELL",
                "quantity": Decimal("0.001"),
                "price": Decimal("110000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
            }
        ]
        output = tmp_path / "koinly.csv"
        exporter.export(trades, str(output))
        assert output.exists()
        content = output.read_text()
        assert "BTC" in content
        assert "Sent Currency" in content

    def test_export_multiple_trades(self, tmp_path):
        from src.tax.koinly_exporter import KoinlyExporter

        exporter = KoinlyExporter(bot_id="bot_a")
        trades = [
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "quantity": Decimal("0.001"),
                "price": Decimal("100000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
            },
            {
                "symbol": "ETH/USDT",
                "side": "SELL",
                "quantity": Decimal("0.1"),
                "price": Decimal("3000"),
                "timestamp": datetime(2025, 5, 20, 13, 0, tzinfo=timezone.utc),
            },
        ]
        output = tmp_path / "koinly_multi.csv"
        exporter.export(trades, str(output))
        assert output.exists()
        content = output.read_text()
        assert "BTC" in content
        assert "ETH" in content


# ---------------------------------------------------------------------------
#  FBRLedger
# ---------------------------------------------------------------------------


class TestFBRLedger:
    """3 tests — covers trade recording, PKR conversion, export."""

    def test_record_trade(self):
        from src.tax.fbr_ledger import FBRLedger

        ledger = FBRLedger(bot_id="bot_a")
        entry = ledger.record_trade(
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "quantity": Decimal("0.001"),
                "price": Decimal("100000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
            }
        )
        assert entry["bot_id"] == "bot_a"
        assert entry["symbol"] == "BTC/USDT"
        assert entry["side"] == "BUY"
        assert "value_pkr" in entry
        assert "value_usd" in entry
        # value_usd = 0.001 * 100000 = 100
        assert Decimal(entry["value_usd"]) == Decimal("100")
        # value_pkr = 100 * 278.50 = 27850
        assert Decimal(entry["value_pkr"]) == Decimal("100") * Decimal("278.50")

    def test_record_sell_trade(self):
        from src.tax.fbr_ledger import FBRLedger

        ledger = FBRLedger(bot_id="bot_a")
        entry = ledger.record_trade(
            {
                "symbol": "ETH/USDT",
                "side": "SELL",
                "quantity": Decimal("0.1"),
                "price": Decimal("3000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
            }
        )
        assert entry["side"] == "SELL"
        assert Decimal(entry["value_usd"]) == Decimal("300")
        # net_value_pkr for SELL = value_pkr - fee_pkr
        assert "net_value_pkr" in entry

    def test_export_ledger(self, tmp_path):
        from src.tax.fbr_ledger import FBRLedger

        ledger = FBRLedger(bot_id="bot_a")
        ledger.record_trade(
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "quantity": Decimal("0.001"),
                "price": Decimal("100000"),
                "timestamp": datetime(2025, 5, 20, 12, 0, tzinfo=timezone.utc),
            }
        )
        output = tmp_path / "fbr.csv"
        ledger.export(str(output))
        assert output.exists()
        content = output.read_text()
        assert "bot_id" in content
        assert "bot_a" in content
        assert "BTC/USDT" in content


# ---------------------------------------------------------------------------
#  PnLCalculator
# ---------------------------------------------------------------------------


class TestPnLCalculator:
    """4 tests — covers FIFO buy/sell, partial sell, multi-symbol."""

    def test_fifo_buy_sell(self):
        from src.tax.pnl_calculator import PnLCalculator

        calc = PnLCalculator()
        calc.record_buy("BTC/USDT", Decimal("0.01"), Decimal("50000"))
        pnl = calc.record_sell("BTC/USDT", Decimal("0.01"), Decimal("55000"))
        # Profit = 0.01 * (55000 - 50000) = 50
        assert pnl == Decimal("50")

    def test_fifo_partial_sell(self):
        from src.tax.pnl_calculator import PnLCalculator

        calc = PnLCalculator()
        calc.record_buy("BTC/USDT", Decimal("0.02"), Decimal("50000"))
        pnl = calc.record_sell("BTC/USDT", Decimal("0.01"), Decimal("55000"))
        assert pnl == Decimal("50")
        # Remaining position: 0.01 BTC at $50k
        remaining_qty = calc.positions["BTC/USDT"][0][0]
        remaining_cost = calc.positions["BTC/USDT"][0][1]
        assert remaining_qty == Decimal("0.01")
        assert remaining_cost == Decimal("50000")

    def test_sell_without_position_returns_zero(self):
        from src.tax.pnl_calculator import PnLCalculator

        calc = PnLCalculator()
        pnl = calc.record_sell("BTC/USDT", Decimal("0.01"), Decimal("55000"))
        assert pnl == Decimal("0")

    def test_multiple_buy_lots_fifo(self):
        from src.tax.pnl_calculator import PnLCalculator

        calc = PnLCalculator()
        # Two separate buy lots
        calc.record_buy("BTC/USDT", Decimal("0.01"), Decimal("40000"))
        calc.record_buy("BTC/USDT", Decimal("0.01"), Decimal("50000"))
        # Sell 0.015 — should consume all of first lot (0.01 @ 40k) and half of second (0.005 @ 50k)
        pnl = calc.record_sell("BTC/USDT", Decimal("0.015"), Decimal("60000"))
        # PnL = 0.01 * (60000 - 40000) + 0.005 * (60000 - 50000) = 200 + 50 = 250
        assert pnl == Decimal("250")


# ---------------------------------------------------------------------------
#  CryptoPanicClient
# ---------------------------------------------------------------------------


class TestCryptoPanicClient:
    """3 tests — covers successful fetch, error handling, close."""

    @pytest.mark.asyncio
    async def test_get_posts_success(self):
        from src.news.cryptopanic_client import CryptoPanicClient

        client = CryptoPanicClient(api_key="test_key")
        client.client = MagicMock()
        client.client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(
                    return_value={
                        "results": [
                            {"title": "Bitcoin surges", "url": "http://example.com/1"},
                            {"title": "Ethereum update", "url": "http://example.com/2"},
                        ]
                    }
                ),
            )
        )
        posts = await client.get_posts(currencies="BTC,ETH", limit=2)
        assert len(posts) == 2
        assert posts[0]["title"] == "Bitcoin surges"
        client.client.get.assert_awaited_once()
        _args, kwargs = client.client.get.call_args
        assert "cryptopanic.com/api/v1" in _args[0]

    @pytest.mark.asyncio
    async def test_get_posts_error_returns_empty(self):
        from src.news.cryptopanic_client import CryptoPanicClient

        client = CryptoPanicClient(api_key="test_key")
        client.client = MagicMock()
        client.client.get = AsyncMock(side_effect=Exception("API error"))
        posts = await client.get_posts()
        assert posts == []

    @pytest.mark.asyncio
    async def test_close(self):
        from src.news.cryptopanic_client import CryptoPanicClient

        client = CryptoPanicClient(api_key="test_key")
        client.client = MagicMock()
        client.client.aclose = AsyncMock()
        await client.close()
        client.client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
#  RSSFetcher
# ---------------------------------------------------------------------------


class TestRSSFetcher:
    """3 tests — covers feed fetch, error handling, close."""

    @pytest.mark.asyncio
    async def test_fetch_all(self):
        from src.news.rss_fetcher import RSSFetcher

        fetcher = RSSFetcher()
        fetcher.client = MagicMock()

        # Mock RSS XML response
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel><title>CoinDesk</title>
    <item>
      <title>Bitcoin breaks new high</title>
      <link>https://example.com/1</link>
      <pubDate>Mon, 20 May 2025 12:00:00 GMT</pubDate>
      <description>Markets rally</description>
    </item>
  </channel>
</rss>"""
        fetcher.client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                text=rss_xml,
            )
        )

        articles = await fetcher.fetch_all(limit=5)
        assert len(articles) > 0
        assert articles[0]["title"] == "Bitcoin breaks new high"
        assert "source" in articles[0]
        assert "link" in articles[0]

    @pytest.mark.asyncio
    async def test_fetch_feed_error_continues(self):
        from src.news.rss_fetcher import RSSFetcher

        fetcher = RSSFetcher()
        fetcher.client = MagicMock()
        fetcher.client.get = AsyncMock(side_effect=Exception("Connection timeout"))
        # Should not raise — errors are caught per-feed
        articles = await fetcher.fetch_all(limit=5)
        assert articles == []

    @pytest.mark.asyncio
    async def test_close(self):
        from src.news.rss_fetcher import RSSFetcher

        fetcher = RSSFetcher()
        fetcher.client = MagicMock()
        fetcher.client.aclose = AsyncMock()
        await fetcher.close()
        fetcher.client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
#  HaltDecision
# ---------------------------------------------------------------------------


class TestHaltDecision:
    """5 tests — covers good news, hack keywords, negative sentiment,
    insufficient articles, mixed articles."""

    def test_no_halt_on_good_news(self):
        from src.news.halt_decision import HaltDecision

        hd = HaltDecision()
        should_halt, reason = hd.should_halt(
            [
                {"title": "Bitcoin price rises steadily"},
                {"title": "New exchange launches features"},
                {"title": "Market analysis shows growth"},
                {"title": "Developers improve protocol"},
                {"title": "Adoption continues globally"},
            ]
        )
        assert should_halt is False

    def test_halt_on_hack_news(self):
        from src.news.halt_decision import HaltDecision

        hd = HaltDecision()
        should_halt, reason = hd.should_halt(
            [
                {"title": "Major exchange hacked, millions stolen"},
                {"title": "Security breach affects thousands"},
                {"title": "Investigators probe hack"},
                {"title": "Users warned to withdraw"},
                {"title": "Emergency shutdown initiated"},
            ]
        )
        assert should_halt is True
        assert "halt_keywords" in reason or "halt" in reason.lower()

    def test_halt_on_negative_sentiment(self):
        from src.news.halt_decision import HaltDecision

        hd = HaltDecision()
        should_halt, reason = hd.should_halt(
            [
                {"title": "Bitcoin crashes to new lows"},
                {"title": "Market panic selling continues"},
                {"title": "Investors lose confidence"},
                {"title": "Regulators announce crackdown"},
                {"title": "Exchange faces insolvency"},
            ]
        )
        assert should_halt is True

    def test_insufficient_articles(self):
        from src.news.halt_decision import HaltDecision

        hd = HaltDecision(min_articles=10)
        should_halt, reason = hd.should_halt([{"title": "One article"}])
        assert should_halt is False
        assert reason == "insufficient_articles"

    def test_no_halt_on_mixed_moderate_news(self):
        from src.news.halt_decision import HaltDecision

        hd = HaltDecision()
        # Mixed but not overwhelmingly negative
        should_halt, reason = hd.should_halt(
            [
                {"title": "Bitcoin price update"},
                {"title": "Market analysis report"},
                {"title": "New features interview"},
                {"title": "Trading volume review"},
                {"title": "Weekly podcast episode"},
            ]
        )
        assert should_halt is False


# ---------------------------------------------------------------------------
#  PrometheusExporter
# ---------------------------------------------------------------------------


class TestPrometheusExporter:
    """3 tests — covers all metric update methods, trade recording,
    alert recording."""

    def test_update_metrics(self):
        from src.reporting.prometheus_exporter import PrometheusExporter

        # Use __new__ to skip __init__ which calls start_http_server
        pe = PrometheusExporter.__new__(PrometheusExporter)
        pe.bot_id = "test"

        # Mock all gauges and counters
        pe.equity = MagicMock()
        pe.drawdown = MagicMock()
        pe.daily_pnl = MagicMock()
        pe.win_rate = MagicMock()
        pe.open_positions = MagicMock()
        pe.signal_strength = MagicMock()
        pe.kill_switch = MagicMock()
        pe.heartbeat_age = MagicMock()
        pe.recon_drift = MagicMock()
        pe.trades_total = MagicMock()
        pe.alerts_total = MagicMock()

        pe.update_equity(520.5)
        pe.update_drawdown(5.2)
        pe.update_daily_pnl(2.35)
        pe.update_win_rate(62.5)
        pe.update_open_positions(3)
        pe.update_signal_strength(0.75)
        pe.update_kill_switch(0)
        pe.update_heartbeat_age(10.5)
        pe.update_reconciliation_drift(1.25)
        pe.record_trade("BUY", "BTC/USDT")
        pe.record_alert("CRITICAL")

        pe.equity.labels.assert_called_with(bot_id="test")
        pe.drawdown.labels.assert_called_with(bot_id="test")
        pe.kill_switch.labels.assert_called_with(bot_id="test")
        pe.trades_total.labels.assert_called_with(
            bot_id="test", side="BUY", symbol="BTC/USDT"
        )
        pe.alerts_total.labels.assert_called_with(bot_id="test", severity="CRITICAL")

    def test_record_trade_increments_counter(self):
        from src.reporting.prometheus_exporter import PrometheusExporter

        pe = PrometheusExporter.__new__(PrometheusExporter)
        pe.bot_id = "test"
        pe.trades_total = MagicMock()
        pe.record_trade("SELL", "ETH/USDT")
        pe.trades_total.labels.assert_called_with(
            bot_id="test", side="SELL", symbol="ETH/USDT"
        )
        pe.trades_total.labels.return_value.inc.assert_called_once()

    def test_update_kill_switch_states(self):
        from src.reporting.prometheus_exporter import PrometheusExporter

        pe = PrometheusExporter.__new__(PrometheusExporter)
        pe.bot_id = "test"
        pe.kill_switch = MagicMock()
        for state in [0, 1, 2]:
            pe.update_kill_switch(state)
        assert pe.kill_switch.labels.call_count == 3


# ---------------------------------------------------------------------------
#  DailyReport
# ---------------------------------------------------------------------------


class TestDailyReport:
    """4 tests — covers daily report generation, retirement checks,
    report content with trades."""

    @pytest.fixture
    def mock_state(self):
        """Mock StateManager for DailyReport tests."""
        state = MagicMock()
        return state

    def test_generate_daily_no_trades(self, mock_state):
        from src.reporting.daily_report import DailyReport

        mock_state.get_recent_trades.return_value = []
        mock_state.get_equity_curve.return_value = []

        report = DailyReport(mock_state, bot_id="bot_a")
        text = report.generate_daily()
        assert "Daily Report" in text
        assert "bot_a" in text
        assert "No trades today" in text

    def test_generate_daily_with_trades(self, mock_state):
        from src.reporting.daily_report import DailyReport

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        mock_state.get_recent_trades.return_value = [
            {
                "timestamp": today_start,
                "side": "BUY",
                "symbol": "BTC/USDT",
                "price": "50000",
                "pnl": 0,
            },
            {
                "timestamp": today_start,
                "side": "SELL",
                "symbol": "BTC/USDT",
                "price": "51000",
                "pnl": 10.5,
            },
        ]
        mock_state.get_equity_curve.return_value = [
            {"equity": 510.0},
            {"equity": 500.0},
        ]

        report = DailyReport(mock_state, bot_id="bot_a")
        text = report.generate_daily()
        assert "Total Trades:" in text
        assert "Win Rate:" in text
        assert "BUY BTC/USDT" in text
        assert "SELL BTC/USDT" in text

    def test_check_retirement_sharpe_negative(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=-0.5,
            drawdown_pct=10,
            losing_months=0,
            overrides_30d=0,
            drift_hours=0,
        )
        assert should_retire is True
        assert "Sharpe" in reason

    def test_check_retirement_drawdown_high(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=1.5,
            drawdown_pct=25,
            losing_months=0,
            overrides_30d=0,
            drift_hours=0,
        )
        assert should_retire is True
        assert "Drawdown" in reason

    def test_check_retirement_losing_months(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=1.5,
            drawdown_pct=10,
            losing_months=4,
            overrides_30d=0,
            drift_hours=0,
        )
        assert should_retire is True
        assert "losing months" in reason

    def test_check_retirement_overrides(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=1.5,
            drawdown_pct=10,
            losing_months=0,
            overrides_30d=10,
            drift_hours=0,
        )
        assert should_retire is True
        assert "overrides" in reason

    def test_check_retirement_drift(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=1.5,
            drawdown_pct=10,
            losing_months=0,
            overrides_30d=0,
            drift_hours=30,
        )
        assert should_retire is True
        assert "Drift" in reason

    def test_check_retirement_no_trigger(self):
        from src.reporting.daily_report import DailyReport

        report = DailyReport(MagicMock(), bot_id="bot_a")
        should_retire, reason = report.check_retirement_conditions(
            sharpe=1.5,
            drawdown_pct=10,
            losing_months=0,
            overrides_30d=0,
            drift_hours=0,
        )
        assert should_retire is False
        assert reason == ""

"""Tests for exchange connectivity."""
import pytest
from decimal import Decimal


class TestImports:
    def test_exchange_module_imports(self):
        import src.exchange
        assert hasattr(src.exchange, '__version__')

    def test_binance_client_import(self):
        from src.exchange.binance_client import BinanceClient
        assert BinanceClient is not None

    def test_paper_broker_import(self):
        from src.exchange.paper_broker import PaperBroker
        assert PaperBroker is not None


class TestBinanceClient:
    def test_client_initialization(self):
        from src.exchange.binance_client import BinanceClient
        client = BinanceClient(api_key="test_key", api_secret="test_secret", testnet=True)
        assert client.testnet == True
        assert client.api_key == "test_key"

    def test_client_base_url_testnet(self):
        from src.exchange.binance_client import BinanceClient
        client = BinanceClient(api_key="k", api_secret="s", testnet=True)
        assert "testnet" in client.base_url


class TestPaperBroker:
    def test_paper_broker_initialization(self):
        from src.exchange.paper_broker import PaperBroker
        broker = PaperBroker(initial_balance=Decimal("500"))
        assert broker.get_balance("USDT") == Decimal("500")

    def test_paper_order_execution(self):
        from src.exchange.paper_broker import PaperBroker
        broker = PaperBroker(initial_balance=Decimal("500"))
        result = broker.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))
        assert result["status"] == "filled"
        assert result["symbol"] == "BTC/USDT"

    def test_paper_balance_after_buy(self):
        from src.exchange.paper_broker import PaperBroker
        broker = PaperBroker(initial_balance=Decimal("500"))
        broker.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))
        usdt_balance = broker.get_balance("USDT")
        assert usdt_balance < Decimal("500")
        assert usdt_balance == Decimal("400")  # 500 - 0.001 * 100000

    def test_paper_balance_after_sell(self):
        from src.exchange.paper_broker import PaperBroker
        broker = PaperBroker(initial_balance=Decimal("500"))
        # First buy
        broker.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))
        # Then sell
        result = broker.place_market_order("BTC/USDT", "SELL", Decimal("0.001"), Decimal("110000"))
        assert result["status"] == "filled"
        assert broker.get_balance("USDT") == Decimal("510")  # 400 + 0.001 * 110000

    def test_trade_history(self):
        from src.exchange.paper_broker import PaperBroker
        broker = PaperBroker(initial_balance=Decimal("500"))
        broker.place_market_order("BTC/USDT", "BUY", Decimal("0.001"), Decimal("100000"))
        assert len(broker.trade_history) == 1

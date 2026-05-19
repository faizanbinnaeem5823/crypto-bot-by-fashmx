"""Tests for order execution."""
import pytest
from decimal import Decimal


class TestImports:
    def test_execution_module_imports(self):
        import src.execution
        assert hasattr(src.execution, '__version__')

    def test_order_manager_import(self):
        from src.execution.order_manager import OrderManager
        assert OrderManager is not None


class TestOrderManager:
    def test_order_manager_initialization(self):
        from src.execution.order_manager import OrderManager
        om = OrderManager(bot_id="test_bot", paper=True)
        assert om.bot_id == "test_bot"
        assert om.paper == True

    def test_validate_order_size(self):
        from src.execution.order_manager import OrderManager
        om = OrderManager(bot_id="test_bot", paper=True)
        assert om.validate_order_size(Decimal("0.001"), "BTC/USDT") == True
        assert om.validate_order_size(Decimal("0.000001"), "BTC/USDT") == False  # Below minimum

    def test_validate_order_size_eth(self):
        from src.execution.order_manager import OrderManager
        om = OrderManager(bot_id="test_bot", paper=True)
        assert om.validate_order_size(Decimal("0.001"), "ETH/USDT") == True

    def test_validate_order_size_default_min(self):
        from src.execution.order_manager import OrderManager
        om = OrderManager(bot_id="test_bot", paper=True)
        # Unknown symbol falls back to default minimum
        assert om.validate_order_size(Decimal("0.001"), "SOL/USDT") == True
        assert om.validate_order_size(Decimal("0.000001"), "SOL/USDT") == False

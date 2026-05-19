"""Tests for state management."""
import pytest
from decimal import Decimal
from datetime import datetime, timezone


class TestImports:
    def test_state_module_imports(self):
        import src.state
        assert hasattr(src.state, '__version__')

    def test_state_manager_import(self):
        from src.state.state_manager import StateManager
        assert StateManager is not None

    def test_trade_dataclass_import(self):
        from src.state.state_manager import Trade
        assert Trade is not None


class TestStateManager:
    def test_initialization(self, temp_db):
        from src.state.state_manager import StateManager
        sm = StateManager(db_path=temp_db, bot_id="test_bot")
        assert sm.bot_id == "test_bot"
        sm.close()

    def test_record_and_retrieve_trade(self, temp_db):
        from src.state.state_manager import StateManager, Trade
        sm = StateManager(db_path=temp_db, bot_id="test_bot")
        trade = Trade(
            id=1, bot_id="test_bot", symbol="BTC/USDT",
            side="BUY", quantity=Decimal("0.001"), price=Decimal("100000"),
            timestamp=datetime.now(timezone.utc), pnl=None, status="open"
        )
        sm.record_trade(trade)
        trades = sm.get_recent_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BTC/USDT"
        sm.close()

    def test_equity_tracking(self, temp_db):
        from src.state.state_manager import StateManager
        sm = StateManager(db_path=temp_db, bot_id="test_bot")
        sm.update_equity(Decimal("500.00"), Decimal("0.00"))
        sm.update_equity(Decimal("505.00"), Decimal("0.00"))
        curve = sm.get_equity_curve(limit=10)
        assert len(curve) == 2
        sm.close()

    def test_heartbeat(self, temp_db):
        from src.state.state_manager import StateManager
        sm = StateManager(db_path=temp_db, bot_id="test_bot")
        sm.heartbeat()
        age = sm.get_heartbeat_age()
        assert age < 5.0  # Should be very recent
        sm.close()

    def test_multiple_trades(self, temp_db):
        from src.state.state_manager import StateManager, Trade
        sm = StateManager(db_path=temp_db, bot_id="test_bot")
        for i in range(3):
            trade = Trade(
                id=i+1, bot_id="test_bot", symbol="ETH/USDT",
                side="SELL", quantity=Decimal("0.01"), price=Decimal("3000"),
                timestamp=datetime.now(timezone.utc), pnl=Decimal("5.00"), status="closed"
            )
            sm.record_trade(trade)
        trades = sm.get_recent_trades(limit=10)
        assert len(trades) == 3
        sm.close()

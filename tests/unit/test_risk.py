"""Tests for risk engine."""
import pytest
from decimal import Decimal


class TestImports:
    def test_risk_module_imports(self):
        import src.risk
        assert hasattr(src.risk, '__version__')

    def test_iron_rules_import(self):
        from src.risk.iron_rules import IronRules
        assert IronRules is not None

    def test_circuit_breakers_import(self):
        from src.risk.circuit_breakers import CircuitBreakers
        assert CircuitBreakers is not None

    def test_kill_switch_import(self):
        from src.risk.kill_switch import KillSwitch
        assert KillSwitch is not None

    def test_risk_engine_import(self):
        from src.risk.risk_engine import RiskEngine
        assert RiskEngine is not None

    def test_regime_detector_import(self):
        from src.risk.regime_detector import RegimeDetector
        assert RegimeDetector is not None

    def test_position_sizer_import(self):
        from src.risk.position_sizer import PositionSizer
        assert PositionSizer is not None


class TestIronRules:
    def test_withdrawals_off_rule(self):
        from src.risk.iron_rules import IronRules
        rules = IronRules()
        assert rules.WITHDRAWALS_OFF == True

    def test_risk_profile_default(self):
        from src.risk.iron_rules import IronRules
        rules = IronRules()
        assert rules.DEFAULT_RISK_PROFILE == "conservative"


class TestCircuitBreakers:
    def test_daily_limit_breach(self, mock_risk_config):
        from src.risk.circuit_breakers import CircuitBreakers
        cb = CircuitBreakers(mock_risk_config)
        portfolio_value = Decimal("500")
        daily_pnl = Decimal("-8.00")  # Exceeds 1.5% limit of $7.50
        assert cb.check_daily_limit(portfolio_value, daily_pnl) == False

    def test_daily_limit_ok(self, mock_risk_config):
        from src.risk.circuit_breakers import CircuitBreakers
        cb = CircuitBreakers(mock_risk_config)
        portfolio_value = Decimal("500")
        daily_pnl = Decimal("-5.00")
        assert cb.check_daily_limit(portfolio_value, daily_pnl) == True

    def test_max_drawdown_kill(self, mock_risk_config):
        from src.risk.circuit_breakers import CircuitBreakers
        cb = CircuitBreakers(mock_risk_config)
        peak = Decimal("500")
        current = Decimal("399")  # 20.2% drawdown
        assert cb.check_max_drawdown(peak, current) == True  # Should trigger kill


class TestKillSwitch:
    def test_kill_switch_initial_state(self):
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        assert ks.is_armed() == False

    def test_kill_switch_arm_and_trigger(self):
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        ks.arm()
        assert ks.is_armed() == True
        ks.trigger()
        assert ks.is_triggered() == True

    def test_kill_switch_reset(self):
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        ks.arm()
        assert ks.is_armed() == True
        ks.reset()
        assert ks.is_armed() == False
        assert ks.is_triggered() == False


class TestRiskEngine:
    def test_position_size_within_limit(self, mock_risk_config, mock_bot_config):
        from src.risk.risk_engine import RiskEngine
        engine = RiskEngine(mock_risk_config, mock_bot_config["bot_id"])
        portfolio = Decimal("500")
        size = engine.calculate_position_size(portfolio, signal_strength=0.8)
        max_size = portfolio * Decimal("0.005")  # 0.5% per trade
        assert size <= max_size
        assert size > 0

    def test_position_size_zero_on_weak_signal(self, mock_risk_config, mock_bot_config):
        from src.risk.risk_engine import RiskEngine
        engine = RiskEngine(mock_risk_config, mock_bot_config["bot_id"])
        portfolio = Decimal("500")
        size = engine.calculate_position_size(portfolio, signal_strength=0.1)
        assert size == Decimal("0")  # Too weak, no trade

    def test_check_trade_allowed_when_kill_triggered(self, mock_risk_config, mock_bot_config):
        from src.risk.risk_engine import RiskEngine
        engine = RiskEngine(mock_risk_config, mock_bot_config["bot_id"])
        engine.kill_switch.trigger()
        allowed, reason = engine.check_trade_allowed(Decimal("500"), Decimal("0"))
        assert allowed == False
        assert "Kill switch" in reason


class TestRegimeDetector:
    def test_regime_unknown_with_insufficient_data(self):
        from src.risk.regime_detector import RegimeDetector
        import pandas as pd
        rd = RegimeDetector()
        df = pd.DataFrame({'close': [100.0, 101.0]})
        assert rd.detect(df) == "unknown"

    def test_regime_returns_valid_value(self, sample_candles):
        from src.risk.regime_detector import RegimeDetector
        rd = RegimeDetector()
        regime = rd.detect(sample_candles)
        assert regime in ["volatile", "trending", "ranging", "unknown"]


class TestPositionSizer:
    def test_fixed_fractional(self, mock_risk_config):
        from src.risk.position_sizer import PositionSizer
        ps = PositionSizer(mock_risk_config)
        size = ps.fixed_fractional(Decimal("500"), 0.5)
        assert size == Decimal("2.5")

    def test_kelly_size_zero_on_no_edge(self, mock_risk_config):
        from src.risk.position_sizer import PositionSizer
        ps = PositionSizer(mock_risk_config)
        size = ps.kelly_size(Decimal("500"), win_rate=0.4, avg_win=Decimal("1"), avg_loss=Decimal("1"))
        # With win_rate=0.4, kelly = 0.4 - (0.6 / 1) = -0.2, half_kelly = -0.1, so 0
        assert size == Decimal("0")

"""Tests for trading strategies."""
import pytest
import pandas as pd
import numpy as np
from decimal import Decimal


class TestImports:
    """Strategy module imports."""

    def test_strategies_module_imports(self):
        import src.strategies
        assert hasattr(src.strategies, '__version__')

    def test_base_strategy_import(self):
        from src.strategies.base_strategy import BaseStrategy
        assert BaseStrategy is not None

    def test_ema_crossover_import(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        assert EMACrossoverStrategy is not None

    def test_rsi_macd_import(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        assert RSIMACDStrategy is not None


class TestEMACrossover:
    """EMA Crossover strategy tests."""

    def _make_df(self, n=50):
        """Create a valid OHLCV DataFrame with DatetimeIndex."""
        dates = pd.date_range(start="2024-01-01", periods=n, freq="1h")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        df = pd.DataFrame({
            'open': close - 0.1,
            'high': close + 0.2,
            'low': close - 0.2,
            'close': close,
            'volume': np.random.randint(1000, 5000, n),
        }, index=dates)
        return df

    def test_strategy_initialization(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        strategy = EMACrossoverStrategy(params={"fast": 9, "slow": 21})
        assert strategy.fast == 9
        assert strategy.slow == 21

    def test_default_parameters(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        defaults = EMACrossoverStrategy.default_parameters()
        assert "fast" in defaults
        assert "slow" in defaults
        assert defaults["fast"] == 9
        assert defaults["slow"] == 21

    def test_generate_signals(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        strategy = EMACrossoverStrategy(params={"fast": 5, "slow": 15})
        df = self._make_df(50)
        signals = strategy.generate_signals(df)
        assert "entry" in signals.columns
        assert "exit" in signals.columns
        assert signals["entry"].dtype == bool
        assert signals["exit"].dtype == bool

    def test_signal_with_insufficient_data_raises(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        strategy = EMACrossoverStrategy(params={"fast": 5, "slow": 15})
        small_df = self._make_df(5)
        with pytest.raises(ValueError):
            strategy.generate_signals(small_df)

    def test_get_name(self):
        from src.strategies.ema_crossover import EMACrossoverStrategy
        strategy = EMACrossoverStrategy()
        assert strategy.get_name() == "EMA_Cross"


class TestRSIMACD:
    """RSI+MACD combo strategy tests."""

    def _make_df(self, n=50):
        """Create a valid OHLCV DataFrame with DatetimeIndex."""
        dates = pd.date_range(start="2024-01-01", periods=n, freq="1h")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        df = pd.DataFrame({
            'open': close - 0.1,
            'high': close + 0.2,
            'low': close - 0.2,
            'close': close,
            'volume': np.random.randint(1000, 5000, n),
        }, index=dates)
        return df

    def test_strategy_initialization(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        strategy = RSIMACDStrategy(params={"rsi_period": 14, "macd_fast": 12, "macd_slow": 26})
        assert strategy.rsi_period == 14
        assert strategy.macd_fast == 12
        assert strategy.macd_slow == 26

    def test_default_parameters(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        defaults = RSIMACDStrategy.default_parameters()
        assert "rsi_period" in defaults
        assert "macd_fast" in defaults
        assert "macd_slow" in defaults
        assert defaults["rsi_period"] == 14

    def test_generate_signals(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        strategy = RSIMACDStrategy(params={"rsi_period": 7, "macd_fast": 8, "macd_slow": 17})
        df = self._make_df(50)
        signals = strategy.generate_signals(df)
        assert "entry" in signals.columns
        assert "exit" in signals.columns
        assert "rsi" in signals.columns
        assert signals["entry"].dtype == bool
        assert signals["exit"].dtype == bool

    def test_calculate_rsi(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        close = pd.Series([100, 101, 102, 101, 100, 99, 98, 99, 100, 101])
        rsi = RSIMACDStrategy.calculate_rsi(close, period=5)
        assert len(rsi) == len(close)
        assert not pd.isna(rsi.iloc[-1])

    def test_calculate_macd(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        close = pd.Series(range(50, 100))
        macd_df = RSIMACDStrategy.calculate_macd(close, fast=12, slow=26, signal=9)
        assert "macd_line" in macd_df.columns
        assert "signal_line" in macd_df.columns
        assert "histogram" in macd_df.columns

    def test_get_name(self):
        from src.strategies.rsi_macd_combo import RSIMACDStrategy
        strategy = RSIMACDStrategy()
        assert strategy.get_name() == "RSI_MACD"

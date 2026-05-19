"""Tests for reporting module."""
import pytest


class TestImports:
    def test_reporting_module_imports(self):
        import src.reporting
        assert hasattr(src.reporting, '__version__')

    def test_metrics_reporter_import(self):
        from src.reporting.metrics_reporter import MetricsReporter
        assert MetricsReporter is not None


class TestMetricsReporter:
    def test_initialization(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        assert mr.bot_id == "test_bot"

    def test_calculate_sharpe_insufficient_data(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        result = mr.calculate_sharpe([])
        assert result == 0.0
        result = mr.calculate_sharpe([0.01])
        assert result == 0.0

    def test_calculate_sharpe_with_data(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        returns = [0.01, 0.02, -0.01, 0.015, 0.005, -0.005, 0.01, 0.02]
        result = mr.calculate_sharpe(returns)
        assert isinstance(result, float)

    def test_calculate_max_drawdown(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        equity = [500, 510, 505, 520, 515, 510, 525, 500]
        mdd = mr.calculate_max_drawdown(equity)
        assert mdd >= 0.0
        # Max drawdown from 525 to 500 = 25/525 = ~4.76%
        assert mdd > 0

    def test_calculate_win_rate(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        trades = [{"pnl": 10}, {"pnl": -5}, {"pnl": 15}, {"pnl": 0}, {"pnl": 20}]
        wr = mr.calculate_win_rate(trades)
        assert wr == 60.0  # 3 wins out of 5

    def test_calculate_win_rate_empty(self):
        from src.reporting.metrics_reporter import MetricsReporter
        mr = MetricsReporter(bot_id="test_bot")
        wr = mr.calculate_win_rate([])
        assert wr == 0.0

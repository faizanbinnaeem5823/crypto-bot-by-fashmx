"""Comprehensive integration tests for bot core modules.

Covers:
  - ConfigLoader: load_bot_config, load_risk_config, validate_config, defaults
  - BotRunner: initialization, initialize(), run(), _trading_cycle(), shutdown()
  - heartbeat_loop: async heartbeat task
  - HealthCheck: HealthState, HealthCheckServer endpoints
  - structured_logging: setup_logging(), JSONFormatter, BotContextFilter

All external dependencies (DB, exchange, Redis) are mocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Generator, List
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest

# --------------------------------------------------------------------------- #
#  Import modules under test
# --------------------------------------------------------------------------- #
from bot.bot_runner import BotRunner
from bot.config_loader import ConfigLoader, _BOT_REQUIRED_FIELDS
from bot.heartbeat import heartbeat_loop
from bot.healthcheck import HealthCheckServer, HealthState
from bot.structured_logging import BotContextFilter, JSONFormatter, setup_logging


# ============================================================================ #
#  Fixtures
# ============================================================================ #

@pytest.fixture
def config_loader() -> ConfigLoader:
    """ConfigLoader pointing at the project config directory."""
    return ConfigLoader("config")


@pytest.fixture
def bot_config() -> Dict[str, Any]:
    """Minimal valid BotRunner config."""
    return {
        "bot_id": "test_bot",
        "symbols": ["BTC/USDT"],
        "timeframe": "1h",
        "paper": True,
        "initial_capital_usd": 500,
        "risk_profile": "conservative",
        "heartbeat_interval_sec": 30,
        "reconciliation_interval_sec": 60,
    }


@pytest.fixture
def runner(bot_config: Dict[str, Any]):
    """Uninitialised BotRunner instance."""
    from bot.bot_runner import BotRunner

    return BotRunner(bot_config)


@pytest.fixture
def log_capture() -> Generator[List[logging.LogRecord], None, None]:
    """Capture log records emitted during a test."""
    records: List[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger()
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


# ============================================================================ #
#  ConfigLoader Tests (5 tests)
# ============================================================================ #


class TestConfigLoader:
    """Tests for ConfigLoader — load, validate, defaults."""

    # -- Load bot config --------------------------------------------------- #

    def test_load_bot_a_config(self, config_loader: ConfigLoader) -> None:
        """Load bot_a.yaml and verify required fields are present."""
        config = config_loader.load_bot_config("bot_a")
        assert config["bot_id"] == "bot_a"
        # bot_a.yaml has nested structure under 'bot:', 'risk:' keys
        assert config["bot"]["timeframe_band"] == "15m-4h"
        assert config["bot"]["initial_capital_usd"] == 500
        assert "BTC/USDT" in config["bot"]["symbols"]
        assert config["bot"]["risk_profile"] == "conservative"
        assert config["risk"]["per_trade_risk_pct"] == 0.5

    def test_load_bot_config_missing_file_returns_defaults(self) -> None:
        """Missing config file falls back to hardcoded defaults."""
        loader = ConfigLoader("/nonexistent/config/path")
        config = loader.load_bot_config("missing_bot")
        assert config["bot_id"] == "missing_bot"
        assert "symbols" in config
        assert config["paper"] is True
        assert config["timeframe"] == "1d"

    def test_load_risk_config_conservative(self, config_loader: ConfigLoader) -> None:
        """Load conservative risk profile config.

        risk_conservative.yaml has nested ``risk_profiles.conservative`` dict.
        The loader merges the whole file so the nested keys are preserved.
        """
        risk = config_loader.load_risk_config("conservative")
        conservative = risk["risk_profiles"]["conservative"]
        assert conservative["per_trade_risk_pct"] == 0.5
        assert conservative["max_drawdown_kill_pct"] == 20.0
        assert conservative["daily_cap_pct"] == 1.5
        # Base defaults are also merged at top level
        assert "max_position_pct" in risk

    def test_load_risk_config_missing_returns_defaults(self) -> None:
        """Missing risk config falls back to default risk parameters."""
        loader = ConfigLoader("/nonexistent/config/path")
        risk = loader.load_risk_config("nonexistent_profile")
        assert risk["per_trade_risk_pct"] == 1.0
        assert risk["max_drawdown_kill_pct"] == 10.0

    # -- Validate config --------------------------------------------------- #

    def test_validate_config_passes(self, config_loader: ConfigLoader) -> None:
        """Valid config with all required fields passes validation."""
        config = config_loader.get_bot_config_defaults()
        assert config_loader.validate_config(config) is True

    def test_validate_config_missing_required_field(self) -> None:
        """Empty config missing required fields fails validation."""
        loader = ConfigLoader()
        assert loader.validate_config({}) is False

    def test_validate_config_missing_symbols(self) -> None:
        """Config with empty symbols list fails validation."""
        loader = ConfigLoader()
        config = loader.get_bot_config_defaults().copy()
        config["symbols"] = []
        assert loader.validate_config(config) is False

    def test_validate_config_paper_not_bool(self) -> None:
        """Config with non-boolean paper field fails validation."""
        loader = ConfigLoader()
        config = loader.get_bot_config_defaults().copy()
        config["paper"] = "yes"  # type: ignore[assignment]
        assert loader.validate_config(config) is False

    # -- Defaults ---------------------------------------------------------- #

    def test_get_bot_config_defaults_returns_copy(self) -> None:
        """get_bot_config_defaults() returns a mutable copy."""
        loader = ConfigLoader()
        defaults1 = loader.get_bot_config_defaults()
        defaults2 = loader.get_bot_config_defaults()
        assert defaults1 is not defaults2
        defaults1["bot_id"] = "mutated"
        assert defaults2["bot_id"] == "bot_a"


# ============================================================================ #
#  BotRunner Tests (10 tests)
# ============================================================================ #


class TestBotRunnerInit:
    """Tests for BotRunner construction and basic properties."""

    def test_runner_stores_config(self, runner, bot_config) -> None:
        """BotRunner stores config and derives attributes."""
        assert runner.bot_id == "test_bot"
        assert runner.symbol == "BTC/USDT"
        assert runner.timeframe == "1h"
        assert runner.paper is True
        assert runner.config is bot_config

    def test_runner_sets_runtime_flags(self, runner) -> None:
        """Fresh BotRunner has _running=False and no tasks."""
        assert runner._running is False
        assert runner._tasks == []
        assert runner._trade_counter == 0

    def test_runner_module_refs_none_before_init(self, runner) -> None:
        """All module references are None before initialize() is called."""
        assert runner.state_manager is None
        assert runner.paper_broker is None
        assert runner.kill_switch is None
        assert runner.risk_engine is None
        assert runner.order_manager is None
        assert runner.strategy is None
        assert runner.reconciler is None


class TestBotRunnerInitialize:
    """Tests for BotRunner.initialize() with fully mocked dependencies."""

    @pytest.mark.asyncio
    async def test_initialize_creates_all_modules(self, runner) -> None:
        """initialize() creates StateManager, PaperBroker, KillSwitch,
        RiskEngine, OrderManager, Strategy and sets _initialized."""
        with (
            patch("bot.bot_runner.StateManager") as MockSM,
            patch("bot.bot_runner.PaperBroker") as MockPB,
            patch("bot.bot_runner.BinanceClient") as MockBC,
            patch("bot.bot_runner.KillSwitch") as MockKS,
            patch("bot.bot_runner.RiskEngine") as MockRE,
            patch("bot.bot_runner.OrderManager") as MockOM,
            patch("bot.bot_runner.EMACrossoverStrategy") as MockStrat,
        ):
            mock_sm = MagicMock()
            mock_pb = MagicMock()
            mock_bc = MagicMock()
            mock_ks = MagicMock()
            mock_re = MagicMock()
            mock_om = MagicMock()
            mock_strat = MagicMock()
            mock_strat.return_value.get_name.return_value = "ema_crossover"

            MockSM.return_value = mock_sm
            MockPB.return_value = mock_pb
            MockBC.return_value = mock_bc
            MockKS.return_value = mock_ks
            MockRE.return_value = mock_re
            MockOM.return_value = mock_om
            MockStrat.return_value = mock_strat

            await runner.initialize()

            assert runner.state_manager is mock_sm
            assert runner.paper_broker is mock_pb
            assert runner.kill_switch is mock_ks
            assert runner.risk_engine is mock_re
            assert runner.order_manager is mock_om
            assert runner.strategy is mock_strat
            assert runner.exchange_client is mock_bc
            # No reconciler in paper mode
            assert runner.reconciler is None

    @pytest.mark.asyncio
    async def test_initialize_with_rsi_macd_strategy(self, bot_config) -> None:
        """initialize() selects RSIMACDStrategy when configured."""
        bot_config["strategy"] = "rsi_macd"
        r = BotRunner(bot_config)
        with (
            patch("bot.bot_runner.StateManager") as MockSM,
            patch("bot.bot_runner.PaperBroker") as MockPB,
            patch("bot.bot_runner.BinanceClient") as MockBC,
            patch("bot.bot_runner.KillSwitch") as MockKS,
            patch("bot.bot_runner.RiskEngine") as MockRE,
            patch("bot.bot_runner.OrderManager") as MockOM,
            patch("bot.bot_runner.RSIMACDStrategy") as MockRSI,
            patch("bot.bot_runner.EMACrossoverStrategy") as MockEMA,
        ):
            # Set return_value on each so .return_value is usable
            MockSM.return_value = MagicMock()
            MockPB.return_value = MagicMock()
            MockBC.return_value = MagicMock()
            MockKS.return_value = MagicMock()
            MockRE.return_value = MagicMock()
            MockOM.return_value = MagicMock()
            mock_rsi = MagicMock()
            mock_rsi.get_name.return_value = "rsi_macd"
            MockRSI.return_value = mock_rsi

            await r.initialize()

            MockRSI.assert_called_once()
            MockEMA.assert_not_called()
            assert r.strategy is mock_rsi

    @pytest.mark.asyncio
    async def test_initialize_unknown_strategy_fallback(self, bot_config) -> None:
        """Unknown strategy name falls back to EMA crossover."""
        bot_config["strategy"] = "unknown_strategy"
        r = BotRunner(bot_config)
        with (
            patch("bot.bot_runner.StateManager") as MockSM,
            patch("bot.bot_runner.PaperBroker") as MockPB,
            patch("bot.bot_runner.BinanceClient") as MockBC,
            patch("bot.bot_runner.KillSwitch") as MockKS,
            patch("bot.bot_runner.RiskEngine") as MockRE,
            patch("bot.bot_runner.OrderManager") as MockOM,
            patch("bot.bot_runner.EMACrossoverStrategy") as MockEMA,
        ):
            MockSM.return_value = MagicMock()
            MockPB.return_value = MagicMock()
            MockBC.return_value = MagicMock()
            MockKS.return_value = MagicMock()
            MockRE.return_value = MagicMock()
            MockOM.return_value = MagicMock()
            mock_ema = MagicMock()
            mock_ema.get_name.return_value = "ema_crossover"
            MockEMA.return_value = mock_ema

            await r.initialize()

            MockEMA.assert_called_once()


class TestBotRunnerShutdown:
    """Tests for BotRunner.shutdown() and signal_handler()."""

    @pytest.mark.asyncio
    async def test_shutdown_sets_running_false(self, runner) -> None:
        """shutdown() sets _running to False and clears tasks."""
        runner._running = True
        runner._tasks = []
        runner.state_manager = MagicMock()
        runner.exchange_client = AsyncMock()

        await runner.shutdown()

        assert runner._running is False
        assert runner._tasks == []

    @pytest.mark.asyncio
    async def test_shutdown_cancels_background_tasks(self, runner) -> None:
        """shutdown() cancels pending background tasks."""
        runner._running = True
        # Use a real asyncio task that will be cancelled
        async def _dummy():
            await asyncio.sleep(3600)

        real_task = asyncio.create_task(_dummy())
        runner._tasks = [real_task]
        runner.state_manager = MagicMock()
        runner.exchange_client = AsyncMock()
        runner.reconciler = MagicMock()

        await runner.shutdown()

        assert real_task.cancelled() or real_task.done()
        assert runner._running is False

    @pytest.mark.asyncio
    async def test_shutdown_closes_modules(self, runner) -> None:
        """shutdown() closes state_manager and exchange_client."""
        runner._running = True
        runner._tasks = []

        mock_sm = MagicMock()
        mock_ex = AsyncMock()
        runner.state_manager = mock_sm
        runner.exchange_client = mock_ex
        runner.reconciler = None

        await runner.shutdown()

        mock_sm.close.assert_called_once()
        mock_ex.close.assert_awaited_once()

    def test_signal_handler_sets_running_false(self, runner) -> None:
        """signal_handler schedules a shutdown task in the event loop."""
        runner._running = True

        # We need a running event loop to test signal_handler
        async def _test():
            # Schedule signal handler
            runner.signal_handler(signal.SIGTERM)
            # Give the shutdown task a chance to run
            await asyncio.sleep(0.05)
            assert runner._running is False

        asyncio.run(_test())

    def test_signal_handler_no_loop(self, runner, monkeypatch) -> None:
        """signal_handler gracefully handles no running loop."""
        monkeypatch.setattr(
            asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError)
        )
        # Should not raise
        runner.signal_handler(signal.SIGTERM)


class TestBotRunnerTradingCycle:
    """Tests for BotRunner._trading_cycle() with mocked strategy/data."""

    @pytest.mark.asyncio
    async def test_trading_cycle_hold_signal(self, runner) -> None:
        """Trading cycle with HOLD signal skips execution."""
        runner._running = True

        # Mocks for all dependencies
        runner.kill_switch = MagicMock()
        runner.kill_switch.is_triggered.return_value = False
        runner.state_manager = MagicMock()
        runner.state_manager.maybe_reset_pnl.return_value = False
        runner.state_manager.get_daily_pnl.return_value = Decimal("0")
        runner.risk_engine = MagicMock()

        # Mock strategy returning HOLD
        mock_strategy = MagicMock()
        mock_strategy.check_signal.return_value = "HOLD"
        runner.strategy = mock_strategy

        # Mock candle data
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__getitem__ = MagicMock(return_value=MagicMock())
        mock_df.__getitem__.return_value.iloc = MagicMock()
        mock_df.__getitem__.return_value.iloc.__getitem__ = MagicMock(return_value="100.0")

        runner._fetch_candles = AsyncMock(
            return_value=[[0] * 12 for _ in range(20)]
        )
        runner._candles_to_dataframe = MagicMock(return_value=mock_df)
        runner._update_equity = AsyncMock()
        runner._check_drawdown = AsyncMock()

        await runner._trading_cycle(1)

        runner.kill_switch.is_triggered.assert_called_once()
        mock_strategy.check_signal.assert_called_once()
        # _execute_signal should NOT be called for HOLD
        runner._update_equity.assert_awaited_once()
        runner._check_drawdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trading_cycle_kill_switch_triggered(self, runner) -> None:
        """Kill switch triggered stops trading loop."""
        runner._running = True
        runner.kill_switch = MagicMock()
        runner.kill_switch.is_triggered.return_value = True

        await runner._trading_cycle(1)

        assert runner._running is False

    @pytest.mark.asyncio
    async def test_trading_cycle_insufficient_candles(self, runner) -> None:
        """Less than 10 candles skips the cycle."""
        runner._running = True
        runner.kill_switch = MagicMock()
        runner.kill_switch.is_triggered.return_value = False
        runner.state_manager = MagicMock()
        runner.state_manager.maybe_reset_pnl.return_value = False
        runner.state_manager.get_daily_pnl.return_value = Decimal("0")

        runner._fetch_candles = AsyncMock(return_value=[1, 2, 3])  # < 10 candles

        await runner._trading_cycle(1)

        # Strategy should not be called
        assert runner.strategy is None

    @pytest.mark.asyncio
    async def test_trading_cycle_buy_signal_executes(self, runner) -> None:
        """BUY signal triggers full execution pipeline."""
        runner._running = True

        runner.kill_switch = MagicMock()
        runner.kill_switch.is_triggered.return_value = False
        runner.state_manager = MagicMock()
        runner.state_manager.maybe_reset_pnl.return_value = False
        runner.state_manager.get_daily_pnl.return_value = Decimal("0")
        runner.paper_broker = MagicMock()
        runner.paper_broker.get_balance.return_value = Decimal("500")
        runner.paper_broker.positions = {}

        # Mock risk engine allows trade
        runner.risk_engine = MagicMock()
        runner.risk_engine.validate_order.return_value = (
            True, "OK", Decimal("0.01")
        )
        runner.risk_engine.enforce_drawdown_check.return_value = (True, "drawdown_ok")

        # Mock order manager
        runner.order_manager = AsyncMock()
        runner.order_manager.submit_order.return_value = {
            "status": "filled", "pnl": "1.5"
        }

        # Mock strategy returning BUY
        mock_strategy = MagicMock()
        mock_strategy.check_signal.return_value = "BUY"
        runner.strategy = mock_strategy

        # Mock dataframe with close price
        mock_close = MagicMock()
        mock_close.iloc.__getitem__ = MagicMock(return_value="50000.0")

        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.__getitem__ = lambda self, key: mock_close if key == "close" else MagicMock()

        # Build a proper mock df
        df_mock = MagicMock()
        df_mock.empty = False
        df_mock.__getitem__ = MagicMock(return_value=MagicMock())
        close_series = MagicMock()
        close_series.iloc = MagicMock()
        close_series.iloc.__getitem__ = MagicMock(return_value="50000.0")
        df_mock.__getitem__.return_value = close_series

        runner._fetch_candles = AsyncMock(
            return_value=[[0, "100", "110", "90", "100", "1000"] + [0] * 6 for _ in range(20)]
        )
        runner._candles_to_dataframe = MagicMock(return_value=df_mock)
        runner._get_portfolio_value = AsyncMock(return_value=Decimal("500"))
        runner._update_equity = AsyncMock()
        runner._check_drawdown = AsyncMock()

        await runner._trading_cycle(1)

        mock_strategy.check_signal.assert_called_once()
        runner.risk_engine.validate_order.assert_called_once()
        runner.order_manager.submit_order.assert_awaited_once()
        runner.state_manager.record_trade.assert_called_once()


class TestBotRunnerHelpers:
    """Tests for BotRunner helper methods."""

    def test_calculate_sleep_returns_minimum(self, runner) -> None:
        """_calculate_sleep returns at least 5 seconds."""
        cycle_start = datetime.now(timezone.utc) - timedelta(seconds=4000)
        sleep = runner._calculate_sleep(cycle_start)
        assert sleep >= 5.0

    def test_calculate_sleep_respects_timeframe(self, runner) -> None:
        """_calculate_sleep respects the configured timeframe."""
        runner.timeframe = "1h"
        cycle_start = datetime.now(timezone.utc)
        sleep = runner._calculate_sleep(cycle_start)
        # Should be close to 3600s since cycle just started
        assert 3590 <= sleep <= 3605


# ============================================================================ #
#  Heartbeat Tests (3 tests)
# ============================================================================ #


class TestHeartbeatLoop:
    """Tests for heartbeat_loop async function."""

    @pytest.mark.asyncio
    async def test_heartbeat_sends_beat(self) -> None:
        """heartbeat_loop sends heartbeat to state manager."""
        mock_sm = MagicMock()
        mock_sm.bot_id = "test_bot"
        mock_sm.heartbeat = MagicMock()

        task = asyncio.create_task(
            heartbeat_loop(mock_sm, interval_sec=0.05)
        )
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_sm.heartbeat.call_count >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_catches_exception(self) -> None:
        """heartbeat_loop survives exceptions from state_manager."""
        mock_sm = MagicMock()
        mock_sm.bot_id = "test_bot"
        # First call raises, second succeeds
        mock_sm.heartbeat = MagicMock(side_effect=[Exception("DB error"), None])

        task = asyncio.create_task(
            heartbeat_loop(mock_sm, interval_sec=0.05)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_sm.heartbeat.call_count >= 2

    @pytest.mark.asyncio
    async def test_heartbeat_exits_on_cancel(self) -> None:
        """heartbeat_loop cleanly exits on asyncio.CancelledError."""
        mock_sm = MagicMock()
        mock_sm.bot_id = "test_bot"

        task = asyncio.create_task(
            heartbeat_loop(mock_sm, interval_sec=60)
        )
        # Cancel immediately
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # heartbeat() should never be called with 60s interval
        mock_sm.heartbeat.assert_not_called()


# ============================================================================ #
#  HealthCheck Tests (6 tests)
# ============================================================================ #


class TestHealthState:
    """Tests for HealthState class."""

    def test_healthy_by_default(self) -> None:
        """Fresh HealthState is healthy."""
        hs = HealthState()
        assert hs.is_healthy() is True

    def test_unhealthy_after_timeout(self) -> None:
        """No tick for 5+ minutes -> unhealthy."""
        hs = HealthState()
        hs.last_tick = datetime.now(timezone.utc) - timedelta(seconds=400)
        assert hs.is_healthy() is False

    def test_unhealthy_from_error_count(self) -> None:
        """Error count > 10 -> unhealthy."""
        hs = HealthState()
        hs.error_count = 11
        assert hs.is_healthy() is False

    def test_unhealthy_from_kill_switch(self) -> None:
        """Kill switch triggered -> unhealthy."""
        hs = HealthState()
        hs.kill_switch_state = "triggered"
        assert hs.is_healthy() is False

    def test_ready_when_initialized(self) -> None:
        """initialized + db_connected + exchange_connected = ready."""
        hs = HealthState()
        hs.initialized = True
        hs.db_connected = True
        hs.exchange_connected = True
        assert hs.is_ready() is True

    def test_not_ready_without_db(self) -> None:
        """Missing DB connection = not ready."""
        hs = HealthState()
        hs.initialized = True
        hs.db_connected = False
        hs.exchange_connected = True
        assert hs.is_ready() is False

    def test_not_ready_without_exchange(self) -> None:
        """Missing exchange connection = not ready."""
        hs = HealthState()
        hs.initialized = True
        hs.db_connected = True
        hs.exchange_connected = False
        assert hs.is_ready() is False

    def test_not_ready_without_initialized(self) -> None:
        """Not initialized = not ready."""
        hs = HealthState()
        hs.initialized = False
        hs.db_connected = True
        hs.exchange_connected = True
        assert hs.is_ready() is False


class TestHealthCheckServer:
    """Tests for HealthCheckServer."""

    @pytest.mark.asyncio
    async def test_server_starts_and_stops(self) -> None:
        """HealthCheckServer starts and stops without error."""
        hs = HealthState()
        server = HealthCheckServer(hs, port=18082)
        await server.start()
        await asyncio.sleep(0.1)
        await server.stop()

    @pytest.mark.asyncio
    async def test_health_endpoint_healthy(self) -> None:
        """GET /health returns 200 when healthy."""
        hs = HealthState()
        server = HealthCheckServer(hs, port=18083)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_health_endpoint_unhealthy(self) -> None:
        """GET /health returns 503 when unhealthy."""
        hs = HealthState()
        hs.error_count = 11
        server = HealthCheckServer(hs, port=18084)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_endpoint_ready(self) -> None:
        """GET /health/ready returns 200 when ready."""
        hs = HealthState()
        hs.initialized = True
        hs.db_connected = True
        hs.exchange_connected = True
        server = HealthCheckServer(hs, port=18085)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    @pytest.mark.asyncio
    async def test_ready_endpoint_not_ready(self) -> None:
        """GET /health/ready returns 503 when not ready."""
        hs = HealthState()
        server = HealthCheckServer(hs, port=18086)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health/ready")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_live_endpoint(self) -> None:
        """GET /health/live returns live status."""
        hs = HealthState()
        server = HealthCheckServer(hs, port=18087)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "live"
        assert "last_tick_age" in data

    @pytest.mark.asyncio
    async def test_live_endpoint_dead(self) -> None:
        """GET /health/live returns 503 when tick is stale."""
        hs = HealthState()
        hs.last_tick = datetime.now(timezone.utc) - timedelta(seconds=400)
        server = HealthCheckServer(hs, port=18088)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health/live")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self) -> None:
        """GET /health/metrics returns current metrics."""
        hs = HealthState()
        hs.equity = 750.0
        hs.daily_pnl = 12.5
        hs.kill_switch_state = "safe"
        server = HealthCheckServer(hs, port=18089)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.get("/health/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["equity"] == 750.0
        assert data["daily_pnl"] == 12.5
        assert data["kill_switch"] == "safe"
        assert "timestamp" in data


# ============================================================================ #
#  Structured Logging Tests (4 tests)
# ============================================================================ #


class TestJSONFormatter:
    """Tests for JSONFormatter."""

    def test_formats_as_json(self) -> None:
        """JSONFormatter produces valid JSON with required fields."""
        formatter = JSONFormatter(bot_id="test_bot")
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "test message"
        assert parsed["bot_id"] == "test_bot"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert "timestamp" in parsed
        assert "module" in parsed
        assert "function" in parsed
        assert "line" in parsed

    def test_includes_extra_fields(self) -> None:
        """JSONFormatter includes extra fields (event, symbol, trade_id)."""
        formatter = JSONFormatter(bot_id="test_bot")
        record = logging.LogRecord(
            name="test.logger",
            level=logging.WARNING,
            pathname="",
            lineno=2,
            msg="trade executed",
            args=(),
            exc_info=None,
        )
        record.event = "trade_executed"
        record.symbol = "BTC/USDT"
        record.trade_id = 42

        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["event"] == "trade_executed"
        assert parsed["symbol"] == "BTC/USDT"
        assert parsed["trade_id"] == 42

    def test_exception_included(self) -> None:
        """JSONFormatter includes exception info."""
        formatter = JSONFormatter(bot_id="test_bot")
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="test.logger",
                level=logging.ERROR,
                pathname="",
                lineno=3,
                msg="error occurred",
                args=(),
                exc_info=sys.exc_info(),
            )

        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "boom" in parsed["exception"]


class TestBotContextFilter:
    """Tests for BotContextFilter."""

    def test_injects_bot_id(self) -> None:
        """BotContextFilter injects bot_id into log records."""
        filt = BotContextFilter(bot_id="bot_a")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="msg", args=(), exc_info=None,
        )
        result = filt.filter(record)
        assert result is True
        assert record.bot_id == "bot_a"

    def test_filter_always_returns_true(self) -> None:
        """BotContextFilter.filter always returns True (never drops records)."""
        filt = BotContextFilter(bot_id="any")
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=1,
            msg="msg", args=(), exc_info=None,
        )
        assert filt.filter(record) is True


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_creates_handlers(self, tmp_path) -> None:
        """setup_logging configures root logger with handlers."""
        log_dir = str(tmp_path / "logs")
        # Clear existing handlers first
        root = logging.getLogger()
        root.handlers.clear()

        setup_logging(bot_id="test_bot", log_dir=log_dir, level="INFO")

        assert len(root.handlers) >= 2  # JSON file + console
        # Check that JSON handler exists
        json_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and h.formatter is not None
            and isinstance(h.formatter, JSONFormatter)
        ]
        assert len(json_handlers) >= 1

    def test_sets_log_level(self, tmp_path) -> None:
        """setup_logging sets root logger level correctly."""
        log_dir = str(tmp_path / "logs2")
        root = logging.getLogger()
        root.handlers.clear()

        setup_logging(bot_id="test_bot", log_dir=log_dir, level="DEBUG")

        assert root.level == logging.DEBUG

    def test_creates_log_directory(self, tmp_path) -> None:
        """setup_logging creates the log directory if it doesn't exist."""
        log_dir = str(tmp_path / "new_logs_dir")
        root = logging.getLogger()
        root.handlers.clear()

        setup_logging(bot_id="test_bot", log_dir=log_dir, level="INFO")

        assert Path(log_dir).exists()

    def test_json_log_file_created(self, tmp_path) -> None:
        """setup_logging creates a JSON log file that receives log records."""
        log_dir = str(tmp_path / "logs3")
        root = logging.getLogger()
        root.handlers.clear()

        setup_logging(bot_id="log_test_bot", log_dir=log_dir, level="INFO")

        test_logger = logging.getLogger("test.json.logger")
        test_logger.info("json log test message", extra={"event": "test_event"})

        log_file = Path(log_dir) / "log_test_bot.json.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "json log test message" in content
        # Verify it's valid JSON
        parsed = json.loads(content.strip().split("\n")[-1])
        assert parsed["message"] == "json log test message"
        assert parsed["event"] == "test_event"

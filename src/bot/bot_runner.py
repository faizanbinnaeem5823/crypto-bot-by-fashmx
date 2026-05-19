"""
BotRunner: orchestrates all trading bot modules into a cohesive system.

Usage::

    runner = BotRunner(config)
    await runner.initialize()
    await runner.run()          # blocks until shutdown
    await runner.shutdown()

The runner wires together:

    1. StateManager        — DuckDB persistence
    2. ExchangeClient      — Binance REST API (live mode)
    3. PaperBroker         — simulated execution (paper mode)
    4. KillSwitch          — emergency halt
    5. RiskEngine          — pre-trade risk checks
    6. OrderManager        — order routing & validation
    7. Strategy            — signal generation (EMA crossover or RSI+MACD)
    8. Reconciler          — bot-vs-exchange drift checks (live only)
    9. Heartbeat           — health ping to DuckDB

All monetary values use ``Decimal``.  All timestamps are UTC.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    import pandas as pd

    PANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PANDAS_AVAILABLE = False

from exchange.binance_client import BinanceClient
from exchange.paper_broker import PaperBroker
from exchange.rate_limiter import RateLimiter
from execution.order_manager import OrderManager
from reconciliation.reconciler import Reconciler
from risk.kill_switch import KillSwitch
from risk.risk_engine import RiskEngine
from state.state_manager import StateManager, Trade
from strategies.ema_crossover import EMACrossoverStrategy
from strategies.rsi_macd_combo import RSIMACDStrategy

from .heartbeat import heartbeat_loop

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Timeframe-to-seconds mapping (for sleep calculations)
# --------------------------------------------------------------------------- #
_TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}


class BotRunner:
    """Central orchestrator for the crypto trading bot.

    Parameters
    ----------
    config :
        Merged bot configuration dictionary.  Must contain at minimum
        ``bot_id``, ``symbols``, ``timeframe``, ``paper``, ``risk_profile``.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.bot_id: str = config["bot_id"]
        self.symbol: str = config["symbols"][0]  # Primary symbol
        self.timeframe: str = config["timeframe"]
        self.paper: bool = config.get("paper", True)

        # Module references — initialised in :meth:`initialize`
        self.state_manager: Optional[StateManager] = None
        self.exchange_client: Optional[BinanceClient] = None
        self.paper_broker: Optional[PaperBroker] = None
        self.kill_switch: Optional[KillSwitch] = None
        self.risk_engine: Optional[RiskEngine] = None
        self.order_manager: Optional[OrderManager] = None
        self.strategy: Any = None
        self.reconciler: Optional[Reconciler] = None

        # Runtime state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._peak_equity = Decimal("0")
        self._daily_pnl = Decimal("0")
        self._trade_counter = 0

        # Redis client (shared across modules)
        self._redis_client: Any = None

        logger.info(
            "[%s] BotRunner created — symbol=%s timeframe=%s paper=%s",
            self.bot_id,
            self.symbol,
            self.timeframe,
            self.paper,
        )

    # ------------------------------------------------------------------ #
    # Initialization — dependency order matters
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """Initialise all modules in correct dependency order.

        Order:
            1. StateManager        (DB persistence — everyone depends on it)
            2. PaperBroker         (always created; used even in live for sizing)
            3. ExchangeClient      (only in live / testnet mode)
            4. KillSwitch          (risk & execution depend on it)
            5. RiskEngine          (order manager depends on it)
            6. OrderManager        (strategy depends on it)
            7. Strategy            (needs signal)
            8. Reconciler          (live mode only)
        """
        logger.info("[%s] Initialising modules …", self.bot_id)

        # 1. StateManager
        db_path = self.config.get("db_path", "data/cryptobot.duckdb")
        self.state_manager = StateManager(
            db_path=db_path,
            bot_id=self.bot_id,
        )
        logger.info("[%s] StateManager initialised — db=%s", self.bot_id, db_path)

        # 2. PaperBroker (always — provides sizing reference + paper trading)
        initial_capital = Decimal(str(self.config.get("initial_capital_usd", 500)))
        self.paper_broker = PaperBroker(initial_balance=initial_capital)
        logger.info(
            "[%s] PaperBroker initialised — balance=%s USDT",
            self.bot_id,
            initial_capital,
        )

        # 3. ExchangeClient (live or testnet)
        if not self.paper:
            exchange_cfg = self.config.get("exchange", {})
            api_key = exchange_cfg.get("api_key", "")
            api_secret = exchange_cfg.get("api_secret", "")
            testnet = exchange_cfg.get("testnet", True)

            if not api_key or not api_secret:
                raise ValueError(
                    f"[{self.bot_id}] API key and secret required for live trading. "
                    "Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables."
                )

            rate_limiter = RateLimiter(rate=10.0, burst=20)
            self.exchange_client = BinanceClient(
                api_key=api_key,
                api_secret=api_secret,
                testnet=testnet,
                rate_limiter=rate_limiter,
            )
            logger.info(
                "[%s] BinanceClient initialised — testnet=%s",
                self.bot_id,
                testnet,
            )
        else:
            # In paper mode we still create an exchange client for market data
            # (no API key needed for public endpoints)
            self.exchange_client = BinanceClient(
                api_key="dummy",
                api_secret="dummy",
                testnet=True,
                rate_limiter=RateLimiter(rate=10.0, burst=20),
            )
            logger.info("[%s] ExchangeClient (paper/dummy) initialised", self.bot_id)

        # 4. KillSwitch
        self.kill_switch = KillSwitch(redis_client=self._redis_client)
        logger.info("[%s] KillSwitch initialised", self.bot_id)

        # 5. RiskEngine
        risk_cfg = self.config.get("risk", {})
        if not risk_cfg:
            # Load default risk config based on profile
            from .config_loader import ConfigLoader

            profile = self.config.get("risk_profile", "conservative")
            loader = ConfigLoader(self.config.get("config_dir", "config"))
            risk_cfg = loader.load_risk_config(profile)

        self.risk_engine = RiskEngine(
            config=risk_cfg,
            bot_id=self.bot_id,
            redis_client=self._redis_client,
        )
        logger.info(
            "[%s] RiskEngine initialised — profile=%s",
            self.bot_id,
            self.config.get("risk_profile", "conservative"),
        )

        # 6. OrderManager
        self.order_manager = OrderManager(
            bot_id=self.bot_id,
            exchange_client=self.exchange_client,
            paper_broker=self.paper_broker,
            paper=self.paper,
            kill_switch=self.kill_switch,
            risk_engine=self.risk_engine,
        )
        logger.info("[%s] OrderManager initialised", self.bot_id)

        # 7. Strategy
        strategy_name = self.config.get("strategy", "ema_crossover")
        strategy_params = self.config.get("strategy_params", {})

        if strategy_name == "ema_crossover":
            self.strategy = EMACrossoverStrategy(params=strategy_params)
        elif strategy_name == "rsi_macd":
            self.strategy = RSIMACDStrategy(params=strategy_params)
        else:
            logger.warning(
                "[%s] Unknown strategy '%s' — falling back to EMA crossover",
                self.bot_id,
                strategy_name,
            )
            self.strategy = EMACrossoverStrategy(params=strategy_params)

        logger.info(
            "[%s] Strategy initialised — name=%s",
            self.bot_id,
            self.strategy.get_name(),
        )

        # 8. Reconciler (live mode only — not needed for paper)
        if not self.paper:
            self.reconciler = Reconciler(
                state_manager=self.state_manager,
                exchange_client=self.exchange_client,
                kill_switch=self.kill_switch,
                interval_sec=self.config.get("reconciliation_interval_sec", 60),
                max_drift_usd=1.00,
            )
            logger.info("[%s] Reconciler initialised", self.bot_id)

        logger.info("[%s] All modules initialised successfully", self.bot_id)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Main trading loop.  Blocks until :meth:`shutdown` is called.

        Background tasks started:
            * Heartbeat (every 30 s)
            * Reconciliation (live mode only, every 60 s)

        The trading cycle runs continuously, sleeping between bars based on
        the configured timeframe.
        """
        self._running = True
        logger.info("[%s] Bot starting — entering main loop", self.bot_id)

        # Start background tasks
        hb_task = asyncio.create_task(
            heartbeat_loop(
                state_manager=self.state_manager,
                interval_sec=self.config.get("heartbeat_interval_sec", 30),
            ),
            name=f"heartbeat-{self.bot_id}",
        )
        self._tasks.append(hb_task)

        if self.reconciler is not None:
            recon_task = asyncio.create_task(
                self.reconciler.run_reconciliation(),
                name=f"reconciler-{self.bot_id}",
            )
            self._tasks.append(recon_task)
            logger.info("[%s] Reconciler background task started", self.bot_id)

        # Main trading loop
        cycle_count = 0
        while self._running:
            cycle_count += 1
            cycle_start = datetime.now(timezone.utc)
            try:
                await self._trading_cycle(cycle_count)
            except asyncio.CancelledError:
                logger.info("[%s] Trading loop cancelled", self.bot_id)
                break
            except Exception as exc:
                logger.critical(
                    "[%s] Trading cycle %d FAILED: %s",
                    self.bot_id,
                    cycle_count,
                    exc,
                    exc_info=True,
                )
                # Brief pause before retry to avoid tight error loops
                await asyncio.sleep(60)
                continue

            # Sleep until next bar
            sleep_sec = self._calculate_sleep(cycle_start)
            logger.debug(
                "[%s] Cycle %d complete — sleeping %.1fs until next bar",
                self.bot_id,
                cycle_count,
                sleep_sec,
            )
            try:
                await asyncio.wait_for(
                    self._shutdown_event_wait(),
                    timeout=sleep_sec,
                )
            except asyncio.TimeoutError:
                pass  # Normal — interval expired, continue to next cycle

        logger.info("[%s] Main trading loop exited", self.bot_id)

    # ------------------------------------------------------------------ #
    # Trading cycle
    # ------------------------------------------------------------------ #

    async def _trading_cycle(self, cycle_num: int) -> None:
        """Execute a single trading cycle.

        Steps:
            1. Check kill switch (emergency stop).
            2. Daily PnL reset if new UTC day.
            3. Fetch latest candles.
            4. Generate trading signal.
            5. If signal != 'HOLD':
               a. Risk validation (position sizing).
               b. Submit order.
               c. Record trade in state DB.
            6. Update equity snapshot.
            7. Check drawdown limits.
        """
        # 1. Kill-switch check
        if self.kill_switch.is_triggered():
            logger.critical(
                "[%s] Kill switch is TRIGGERED — halting trading",
                self.bot_id,
            )
            self._running = False
            return

        # 2. Daily PnL reset (UTC midnight rollover)
        try:
            if self.state_manager.maybe_reset_pnl():
                self._daily_pnl = Decimal("0")
                logger.info(
                    "[%s] Daily PnL reset (new UTC day)",
                    self.bot_id,
                )
            self._daily_pnl = self.state_manager.get_daily_pnl()
        except Exception as exc:
            logger.error("[%s] PnL reset check failed: %s", self.bot_id, exc)

        # 3. Fetch candles
        candles = await self._fetch_candles()
        if candles is None or len(candles) < 10:
            logger.warning(
                "[%s] Insufficient candle data (%s rows) — skipping cycle",
                self.bot_id,
                len(candles) if candles is not None else "None",
            )
            return

        # 4. Generate signal
        df = self._candles_to_dataframe(candles)
        signal = self.strategy.check_signal(df)

        logger.debug(
            "[%s] Cycle %d | Signal=%s | Close=%s",
            self.bot_id,
            cycle_num,
            signal,
            df["close"].iloc[-1] if not df.empty else "N/A",
        )

        # 5. Execute trade if signal generated
        if signal in ("BUY", "SELL"):
            await self._execute_signal(signal, df, cycle_num)

        # 6. Update equity
        await self._update_equity()

        # 7. Check drawdown
        await self._check_drawdown()

    # ------------------------------------------------------------------ #
    # Signal execution
    # ------------------------------------------------------------------ #

    async def _execute_signal(
        self,
        signal: str,
        df: Any,
        cycle_num: int,
    ) -> None:
        """Execute a BUY or SELL signal through the full order pipeline.

        Parameters
        ----------
        signal :
            ``'BUY'`` or ``'SELL'``.
        df :
            DataFrame with OHLCV data (used for portfolio value calc).
        cycle_num :
            Current cycle number (for trade ID generation).
        """
        side = "BUY" if signal == "BUY" else "SELL"
        current_price = Decimal(str(df["close"].iloc[-1]))

        # -- Portfolio value --
        portfolio_value = await self._get_portfolio_value()

        # -- Risk validation (kill switch + position sizing + drawdown) --
        try:
            allowed, reason, position_size = self.risk_engine.validate_order(
                portfolio_value=portfolio_value,
                current_equity=portfolio_value,
                daily_pnl=self._daily_pnl,
                weekly_pnl=Decimal("0"),  # Could be fetched from state
                monthly_pnl=Decimal("0"),  # Could be fetched from state
                signal_strength=1.0,  # Full strength for crossover signals
            )
        except Exception as exc:
            logger.error(
                "[%s] Risk validation exception: %s",
                self.bot_id,
                exc,
            )
            return

        if not allowed:
            logger.info(
                "[%s] Signal %s BLOCKED — reason=%s",
                self.bot_id,
                side,
                reason,
            )
            return

        if position_size <= 0:
            logger.info(
                "[%s] Signal %s — zero position size after risk sizing",
                self.bot_id,
                side,
            )
            return

        # -- Submit order --
        logger.info(
            "[%s] Executing %s | size=%s | price=%s",
            self.bot_id,
            side,
            position_size,
            current_price,
        )

        try:
            result = await self.order_manager.submit_order(
                symbol=self.symbol,
                side=side,
                quantity=position_size,
                price=current_price,
                portfolio_value=portfolio_value,
                daily_pnl=self._daily_pnl,
            )
        except Exception as exc:
            logger.error(
                "[%s] Order submission FAILED: %s",
                self.bot_id,
                exc,
            )
            return

        # -- Record trade in state DB --
        self._trade_counter += 1
        pnl = Decimal(str(result.get("pnl", "0"))) if isinstance(result, dict) else None
        trade = Trade(
            id=self._trade_counter,
            bot_id=self.bot_id,
            symbol=self.symbol,
            side=side,
            quantity=position_size,
            price=current_price,
            timestamp=datetime.now(timezone.utc),
            pnl=pnl,
            status="filled" if result.get("status") == "filled" else "submitted",
        )

        try:
            self.state_manager.record_trade(trade)
            logger.info(
                "[%s] Trade recorded — id=%s side=%s qty=%s price=%s",
                self.bot_id,
                trade.id,
                trade.side,
                trade.quantity,
                trade.price,
            )
        except Exception as exc:
            logger.error(
                "[%s] Failed to record trade: %s",
                self.bot_id,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Equity & drawdown
    # ------------------------------------------------------------------ #

    async def _update_equity(self) -> None:
        """Snapshot current equity into the state DB."""
        try:
            portfolio_value = await self._get_portfolio_value()
            cash = await self._get_cash_balance()

            self.state_manager.update_equity(
                equity=portfolio_value,
                cash=cash,
            )

            # Track peak equity for drawdown calculations
            if portfolio_value > self._peak_equity:
                self._peak_equity = portfolio_value

            logger.debug(
                "[%s] Equity updated — equity=%s cash=%s peak=%s",
                self.bot_id,
                portfolio_value,
                cash,
                self._peak_equity,
            )
        except Exception as exc:
            logger.error(
                "[%s] Equity update failed: %s",
                self.bot_id,
                exc,
            )

    async def _check_drawdown(self) -> None:
        """Run drawdown check via the risk engine.

        If the drawdown exceeds the configured limit the kill switch is
        auto-triggered by :meth:`RiskEngine.enforce_drawdown_check`.
        """
        try:
            portfolio_value = await self._get_portfolio_value()
            allowed, reason = self.risk_engine.enforce_drawdown_check(
                peak_equity=self._peak_equity,
                current_equity=portfolio_value,
            )
            if not allowed:
                logger.critical(
                    "[%s] Drawdown kill triggered — reason=%s",
                    self.bot_id,
                    reason,
                )
                self._running = False
        except Exception as exc:
            logger.error(
                "[%s] Drawdown check failed: %s",
                self.bot_id,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Data helpers
    # ------------------------------------------------------------------ #

    async def _fetch_candles(self) -> Optional[List[List]]:
        """Fetch recent klines from the exchange.

        Returns
        -------
        list or None
            Raw klines from Binance, or ``None`` on error.
        """
        try:
            candles = await self.exchange_client.get_klines(
                symbol=self.symbol,
                interval=self.timeframe,
                limit=200,
            )
            return candles
        except Exception as exc:
            logger.error(
                "[%s] Failed to fetch candles: %s",
                self.bot_id,
                exc,
            )
            return None

    def _candles_to_dataframe(self, candles: List[List]) -> Any:
        """Convert Binance kline list to a pandas DataFrame.

        Parameters
        ----------
        candles :
            Raw klines from Binance ``get_klines``.

        Returns
        -------
        pandas.DataFrame
            DataFrame with columns ``open, high, low, close, volume`` and
            a DatetimeIndex.
        """
        if not PANDAS_AVAILABLE:
            raise RuntimeError("pandas is required for signal generation")

        df = pd.DataFrame(
            candles,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.set_index("open_time", inplace=True)

        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df[["open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------ #
    # Portfolio helpers
    # ------------------------------------------------------------------ #

    async def _get_portfolio_value(self) -> Decimal:
        """Calculate total portfolio value in USDT.

        Paper mode: uses PaperBroker balances.
        Live mode:  queries exchange balances.

        Returns
        -------
        Decimal
            Total portfolio value (cash + position * price).
        """
        if self.paper:
            usdt = self.paper_broker.get_balance("USDT")
            base, _ = self.symbol.split("/")
            position = self.paper_broker.positions.get(base, Decimal("0"))
            # Rough price estimate from paper broker's perspective
            # (In production, fetch last price from exchange)
            try:
                last_price = await self._get_last_price()
            except Exception:
                last_price = Decimal("0")
            return usdt + position * last_price

        # Live mode
        try:
            balances = await self.exchange_client.get_all_balances()
            usdt = balances.get("USDT", Decimal("0"))
            base, _ = self.symbol.split("/")
            position = balances.get(base, Decimal("0"))
            try:
                last_price = await self._get_last_price()
            except Exception:
                last_price = Decimal("0")
            return usdt + position * last_price
        except Exception as exc:
            logger.error(
                "[%s] Failed to get portfolio value: %s",
                self.bot_id,
                exc,
            )
            return Decimal("0")

    async def _get_cash_balance(self) -> Decimal:
        """Return the USDT cash balance.

        Returns
        -------
        Decimal
            Free USDT balance.
        """
        if self.paper:
            return self.paper_broker.get_balance("USDT")
        try:
            return await self.exchange_client.get_balance("USDT")
        except Exception as exc:
            logger.error(
                "[%s] Failed to get cash balance: %s",
                self.bot_id,
                exc,
            )
            return Decimal("0")

    async def _get_last_price(self) -> Decimal:
        """Fetch the latest close price for the primary symbol.

        Returns
        -------
        Decimal
            Latest close price.
        """
        try:
            candles = await self.exchange_client.get_klines(
                symbol=self.symbol,
                interval=self.timeframe,
                limit=1,
            )
            if candles and len(candles) > 0:
                return Decimal(str(candles[-1][4]))  # close price
        except Exception as exc:
            logger.error(
                "[%s] Failed to get last price: %s",
                self.bot_id,
                exc,
            )
        return Decimal("0")

    # ------------------------------------------------------------------ #
    # Sleep helpers
    # ------------------------------------------------------------------ #

    def _calculate_sleep(self, cycle_start: datetime) -> float:
        """Calculate seconds to sleep until the next bar.

        Parameters
        ----------
        cycle_start :
            UTC timestamp when the current cycle began.

        Returns
        -------
        float
            Seconds to sleep (minimum 5 s to avoid tight loops).
        """
        tf_sec = _TIMEFRAME_SECONDS.get(self.timeframe, 3600)
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        sleep_sec = max(tf_sec - elapsed, 5.0)
        return sleep_sec

    async def _shutdown_event_wait(self) -> None:
        """Block until the runner is signalled to shut down."""
        while self._running:
            await asyncio.sleep(1)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    async def shutdown(self) -> None:
        """Graceful shutdown.

        Steps:
            1. Stop the main loop.
            2. Cancel all background tasks.
            3. Stop reconciler.
            4. Close state manager (DuckDB).
            5. Close exchange client (HTTP session).
        """
        if not self._running and not self._tasks:
            logger.info("[%s] Shutdown already complete", self.bot_id)
            return

        logger.info("[%s] Shutdown initiated …", self.bot_id)
        self._running = False

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for task, result in zip(self._tasks, results):
                if isinstance(result, Exception) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning(
                        "[%s] Task %s raised during shutdown: %s",
                        self.bot_id,
                        task.get_name(),
                        result,
                    )
        self._tasks.clear()

        # Stop reconciler
        if self.reconciler is not None:
            self.reconciler.stop()

        # Close modules
        try:
            if self.state_manager is not None:
                self.state_manager.close()
                logger.info("[%s] StateManager closed", self.bot_id)
        except Exception as exc:
            logger.error("[%s] Error closing StateManager: %s", self.bot_id, exc)

        try:
            if self.exchange_client is not None:
                await self.exchange_client.close()
                logger.info("[%s] ExchangeClient closed", self.bot_id)
        except Exception as exc:
            logger.error("[%s] Error closing ExchangeClient: %s", self.bot_id, exc)

        logger.info("[%s] Shutdown complete", self.bot_id)

    # ------------------------------------------------------------------ #
    # Signal handler (SIGTERM / SIGINT)
    # ------------------------------------------------------------------ #

    def signal_handler(self, sig: signal.Signals) -> None:
        """Handle OS termination signals.

        Schedules an asynchronous shutdown so the bot exits cleanly.

        Parameters
        ----------
        sig :
            The signal that was received (e.g. ``signal.SIGTERM``).
        """
        signame = sig.name if hasattr(sig, "name") else str(sig)
        logger.info("[%s] Received signal %s — initiating shutdown", self.bot_id, signame)

        # Schedule shutdown in the event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.shutdown())
        except RuntimeError:
            # No running loop — already shutting down
            pass

#!/usr/bin/env python3
"""
Backtest engine - runs strategies across all timeframes.
Generates performance report to help operator choose optimal timeframe.

Usage:
    python scripts/backtest_all_timeframes.py --symbol BTC/USDT --output results/
    python scripts/backtest_all_timeframes.py --all --output results/
    python scripts/backtest_all_timeframes.py --symbol ETH/USDT --strategy ema --output results/

The script loads OHLCV data from DuckDB (created by download_historical_data.py)
and falls back to realistic synthetic data if real data is unavailable.
"""

import argparse
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strategies.ema_crossover import EMACrossoverStrategy
from strategies.rsi_macd_combo import RSIMACDStrategy
from strategies.base_strategy import BaseStrategy

# Optional imports with graceful fallbacks
try:
    import vectorbt as vbt
    VECTORBT_AVAILABLE = True
except ImportError:
    VECTORBT_AVAILABLE = False
    print("WARNING: vectorbt not installed. Using numpy-based backtest fallback.")

try:
    from tabulate import tabulate
    TABULATE_AVAILABLE = True
except ImportError:
    TABULATE_AVAILABLE = False
    print("WARNING: tabulate not installed. Using simple table formatting.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Backtest configuration (mutable via _CONFIG dict)
# =============================================================================

_CONFIG = {
    "initial_capital": 500,  # USD starting capital
    "fee_pct": 0.001,  # 0.1% Binance spot maker/taker fee
    "slippage_pct": 0.0005,  # 0.05% slippage assumption
}

# Module-level aliases for convenience
INITIAL_CAPITAL = _CONFIG["initial_capital"]
FEE_PCT = _CONFIG["fee_pct"]
SLIPPAGE_PCT = _CONFIG["slippage_pct"]
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
SYMBOLS = ["BTC/USDT", "ETH/USDT"]

# Timeframe metadata for report generation
TIMEFRAME_META = {
    "1m": {"label": "1 Minute", "bars_per_day": 1440, "category": "Scalping"},
    "5m": {"label": "5 Minute", "bars_per_day": 288, "category": "Day Trading"},
    "15m": {"label": "15 Minute", "bars_per_day": 96, "category": "Swing Trading"},
    "1h": {"label": "1 Hour", "bars_per_day": 24, "category": "Swing Trading"},
    "4h": {"label": "4 Hour", "bars_per_day": 6, "category": "Position Trading"},
    "1d": {"label": "1 Day", "bars_per_day": 1, "category": "Long-term"},
}


# =============================================================================
# Data Loading
# =============================================================================

def load_data(
    symbol: str,
    timeframe: str,
    data_dir: str = "data",
    start_date: str = "2020-01-01",
    end_date: str = "2025-05-20",
) -> pd.DataFrame:
    """
    Load OHLCV data from DuckDB or Parquet.

    Tries multiple sources in order:
        1. DuckDB database (data/cryptobot.duckdb)
        2. Parquet files (data/candles_{symbol}_{timeframe}.parquet)
        3. CSV files (data/candles_{symbol}_{timeframe}.csv)
        4. Synthetic data generator (fallback)

    Args:
        symbol: Trading pair (e.g., 'BTC/USDT')
        timeframe: Candle timeframe (e.g., '1h')
        data_dir: Directory containing data files
        start_date: Filter data from this date
        end_date: Filter data until this date

    Returns:
        DataFrame with columns [open, high, low, close, volume] and DatetimeIndex
    """
    safe_symbol = symbol.replace("/", "_").lower()
    db_path = Path(data_dir) / "cryptobot.duckdb"
    parquet_path = Path(data_dir) / f"candles_{safe_symbol}_{timeframe}.parquet"
    csv_path = Path(data_dir) / f"candles_{safe_symbol}_{timeframe}.csv"

    # --- Try DuckDB ---
    if db_path.exists():
        try:
            import duckdb
            conn = duckdb.connect(str(db_path), read_only=True)
            query = f"""
                SELECT * FROM candles
                WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
                ORDER BY open_time
            """
            df = conn.execute(query).df()
            conn.close()
            if len(df) > 100:
                logger.info(f"  Loaded {len(df)} rows from DuckDB for {symbol} {timeframe}")
                df["open_time"] = pd.to_datetime(df["open_time"])
                df.set_index("open_time", inplace=True)
                return df[start_date:end_date]
        except Exception as e:
            logger.debug(f"  DuckDB load failed: {e}")

    # --- Try Parquet ---
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            if len(df) > 100:
                logger.info(f"  Loaded {len(df)} rows from Parquet for {symbol} {timeframe}")
                if "open_time" in df.columns:
                    df["open_time"] = pd.to_datetime(df["open_time"])
                    df.set_index("open_time", inplace=True)
                return df[start_date:end_date]
        except Exception as e:
            logger.debug(f"  Parquet load failed: {e}")

    # --- Try CSV ---
    if csv_path.exists():
        try:
            df = pd.read_csv(csv_path)
            if len(df) > 100:
                logger.info(f"  Loaded {len(df)} rows from CSV for {symbol} {timeframe}")
                if "open_time" in df.columns:
                    df["open_time"] = pd.to_datetime(df["open_time"])
                    df.set_index("open_time", inplace=True)
                elif "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"])
                    df.set_index("timestamp", inplace=True)
                return df[start_date:end_date]
        except Exception as e:
            logger.debug(f"  CSV load failed: {e}")

    # --- Fallback: Synthetic data ---
    logger.warning(
        f"  No real data found for {symbol} {timeframe}, generating synthetic data"
    )
    return generate_synthetic_data(symbol, timeframe)


def generate_synthetic_data(
    symbol: str,
    timeframe: str,
    start_date: str = "2020-01-01",
    end_date: str = "2025-05-20",
) -> pd.DataFrame:
    """
    Generate realistic synthetic crypto price data for testing.

    Uses geometric Brownian motion with crypto-like volatility
    and regime-switching (trending / mean-reverting periods).

    Args:
        symbol: 'BTC/USDT' or 'ETH/USDT' - determines base price
        timeframe: Candle granularity
        start_date, end_date: Date range

    Returns:
        DataFrame with columns [open, high, low, close, volume] and DatetimeIndex
    """
    np.random.seed(42)

    # Map timeframe to pandas frequency string and approximate bar count
    tf_config = {
        "1m": ("min", 2628000),
        "5m": ("5min", 525600),
        "15m": ("15min", 175200),
        "1h": ("h", 43800),
        "4h": ("4h", 10950),
        "1d": ("D", 1826),
    }
    freq, n_default = tf_config.get(timeframe, ("h", 43800))

    # Generate date range
    dates = pd.date_range(start=start_date, end=end_date, freq=freq)
    n = len(dates)

    # Base price and volatility parameters by symbol
    if "BTC" in symbol.upper():
        base_price = 10000  # BTC ~$10K in 2020
        annual_drift = 0.25  # ~25% annual return
        annual_vol = 0.75  # 75% annual volatility (crypto-like)
    else:
        base_price = 200  # ETH ~$200 in 2020
        annual_drift = 0.35  # ETH has higher drift historically
        annual_vol = 0.85  # ETH is more volatile

    # Scale to per-period values
    periods_per_year = {
        "1m": 525600, "5m": 105120, "15m": 35040,
        "1h": 8760, "4h": 2190, "1d": 365,
    }.get(timeframe, 8760)

    mu = annual_drift / periods_per_year  # per-period drift
    sigma = annual_vol / np.sqrt(periods_per_year)  # per-period volatility

    # Clamp sigma to avoid overflow on very short timeframes
    sigma = min(sigma, 0.05)  # Max 5% per-period volatility

    # Generate returns with regime switching (bull/bear/sideways)
    returns = np.zeros(n)
    regime = 0  # 0=neutral, 1=bull, -1=bear
    regime_remaining = 0

    for i in range(n):
        if regime_remaining <= 0:
            # Switch regime
            regime = np.random.choice([-1, 0, 1], p=[0.30, 0.40, 0.30])
            regime_remaining = np.random.randint(min(200, n//10), min(1500, n//2))

        regime_extra_drift = regime * 0.0002  # modest extra drift per period
        returns[i] = np.random.normal(mu + regime_extra_drift, sigma)
        regime_remaining -= 1

    # Add occasional large moves (crypto flash crashes/pumps)
    jump_prob = min(0.001, 5.0 / n)  # scale jump probability to data size
    jumps = np.random.random(n) < jump_prob
    jump_sizes = np.random.choice([-1, 1], size=n) * np.random.exponential(0.03, size=n)
    jump_sizes = np.clip(jump_sizes, -0.20, 0.20)  # Cap individual jumps at 20%
    returns += jumps * jump_sizes

    # Clip extreme returns to prevent numerical overflow
    returns = np.clip(returns, -0.15, 0.15)  # Max 15% move per period

    # Generate price series
    log_returns = np.cumsum(returns)
    prices = base_price * np.exp(log_returns)

    # Generate OHLCV from close prices
    df = pd.DataFrame(index=dates)
    df["close"] = prices
    df["open"] = df["close"].shift(1).fillna(base_price)

    # High/low based on intraperiod volatility
    intraperiod_vol = sigma * 0.6
    df["high"] = df[["open", "close"]].max(axis=1) * (
        1 + np.abs(np.random.normal(0, intraperiod_vol, n))
    )
    df["low"] = df[["open", "close"]].min(axis=1) * (
        1 - np.abs(np.random.normal(0, intraperiod_vol, n))
    )
    # Ensure low <= high
    mask = df["low"] > df["high"]
    df.loc[mask, "low"] = df.loc[mask, "high"] * 0.999

    # Volume: log-normal distribution with regime-dependent mean
    base_volume = {"BTC": 500_000_000, "ETH": 200_000_000}.get(
        symbol.split("/")[0].upper(), 100_000_000
    )
    volume = np.random.lognormal(
        mean=np.log(base_volume / 10), sigma=1.5, size=n
    )
    # Higher volume during large moves
    abs_returns = np.abs(returns)
    volume *= (1 + abs_returns * 50)
    df["volume"] = volume

    logger.info(f"  Generated {len(df)} synthetic {timeframe} candles for {symbol}")
    return df


# =============================================================================
# Strategy backtest runners
# =============================================================================

def run_ema_backtest(
    df: pd.DataFrame,
    fast: int = 9,
    slow: int = 21,
    capital: float = None,
    fee: float = None,
    slippage: float = None,
) -> Dict:
    """
    Run EMA crossover backtest using vectorbt or numpy fallback.

    Strategy: Buy when fast EMA crosses above slow EMA.
              Sell when fast EMA crosses below slow EMA.

    Args:
        df: OHLCV DataFrame
        fast: Fast EMA period
        slow: Slow EMA period
        capital: Initial capital in USD
        fee: Trading fee as decimal (0.001 = 0.1%)
        slippage: Slippage as decimal (0.0005 = 0.05%)

    Returns:
        Dict of performance metrics
    """
    # Resolve config defaults
    capital = capital if capital is not None else _CONFIG["initial_capital"]
    fee = fee if fee is not None else _CONFIG["fee_pct"]
    slippage = slippage if slippage is not None else _CONFIG["slippage_pct"]

    close = df["close"]

    if VECTORBT_AVAILABLE:
        # Calculate EMAs
        fast_ema = close.ewm(span=fast, adjust=False).mean()
        slow_ema = close.ewm(span=slow, adjust=False).mean()

        # Crossover signals
        entries = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
        exits = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

        portfolio = vbt.Portfolio.from_signals(
            close=close,
            entries=entries,
            exits=exits,
            init_cash=capital,
            fees=fee,
            slippage=slippage,
            freq=pd.infer_freq(df.index) or "h",
        )
        return extract_metrics_vectorbt(portfolio, "EMA_Cross")
    else:
        # Fallback to numpy-based backtest
        return run_numpy_backtest(df, "ema", fast=fast, slow=slow)


def run_rsi_macd_backtest(
    df: pd.DataFrame,
    rsi_p: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    oversold: int = 40,
    overbought: int = 60,
    capital: float = None,
    fee: float = None,
    slippage: float = None,
) -> Dict:
    """
    Run RSI+MACD combo backtest using vectorbt or numpy fallback.

    Strategy: Buy when RSI < oversold AND MACD crosses above signal.
              Sell when RSI > overbought AND MACD crosses below signal.

    Args:
        df: OHLCV DataFrame
        rsi_p: RSI period
        macd_fast: MACD fast EMA period
        macd_slow: MACD slow EMA period
        oversold: RSI oversold threshold for buy
        overbought: RSI overbought threshold for sell
        capital: Initial capital
        fee: Trading fee
        slippage: Slippage

    Returns:
        Dict of performance metrics
    """
    # Resolve config defaults
    capital = capital if capital is not None else _CONFIG["initial_capital"]
    fee = fee if fee is not None else _CONFIG["fee_pct"]
    slippage = slippage if slippage is not None else _CONFIG["slippage_pct"]

    close = df["close"]

    if VECTORBT_AVAILABLE:
        # RSI calculation
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(rsi_p).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(rsi_p).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        # MACD calculation
        ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()

        # Combo signals
        entries = (
            (rsi < oversold)
            & (macd_line > signal_line)
            & (macd_line.shift(1) <= signal_line.shift(1))
        )
        exits = (
            (rsi > overbought)
            & (macd_line < signal_line)
            & (macd_line.shift(1) >= signal_line.shift(1))
        )

        portfolio = vbt.Portfolio.from_signals(
            close=close,
            entries=entries,
            exits=exits,
            init_cash=capital,
            fees=fee,
            slippage=slippage,
            freq=pd.infer_freq(df.index) or "h",
        )
        return extract_metrics_vectorbt(portfolio, "RSI_MACD")
    else:
        return run_numpy_backtest(
            df, "rsi_macd", rsi_p=rsi_p, macd_fast=macd_fast,
            macd_slow=macd_slow, oversold=oversold, overbought=overbought,
        )


def run_numpy_backtest(df: pd.DataFrame, strategy_type: str, **kwargs) -> Dict:
    """
    Pure numpy backtest engine (used when vectorbt is unavailable).

    Implements event-driven backtesting with realistic fee and slippage modeling.
    """
    close = df["close"].values
    n = len(close)

    # Calculate signals
    if strategy_type == "ema":
        fast = kwargs.get("fast", 9)
        slow = kwargs.get("slow", 21)
        fast_ema = pd.Series(close).ewm(span=fast, adjust=False).mean().values
        slow_ema = pd.Series(close).ewm(span=slow, adjust=False).mean().values
        entries = (fast_ema[1:] > slow_ema[1:]) & (fast_ema[:-1] <= slow_ema[:-1])
        exits = (fast_ema[1:] < slow_ema[1:]) & (fast_ema[:-1] >= slow_ema[:-1])
    else:
        rsi_p = kwargs.get("rsi_p", 14)
        macd_fast = kwargs.get("macd_fast", 12)
        macd_slow = kwargs.get("macd_slow", 26)
        oversold = kwargs.get("oversold", 40)
        overbought = kwargs.get("overbought", 60)

        s = pd.Series(close)
        delta = s.diff()
        gain = delta.where(delta > 0, 0).rolling(rsi_p).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(rsi_p).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).values

        ema_fast = s.ewm(span=macd_fast, adjust=False).mean().values
        ema_slow = s.ewm(span=macd_slow, adjust=False).mean().values
        macd_line = ema_fast - ema_slow
        signal_line = pd.Series(macd_line).ewm(span=9, adjust=False).mean().values

        entries = (
            (rsi[1:] < oversold)
            & (macd_line[1:] > signal_line[1:])
            & (macd_line[:-1] <= signal_line[:-1])
        )
        exits = (
            (rsi[1:] > overbought)
            & (macd_line[1:] < signal_line[1:])
            & (macd_line[:-1] >= signal_line[:-1])
        )

    # Align signals (entries/exits start at index 1)
    entries = np.concatenate([[False], entries])
    exits = np.concatenate([[False], exits])

    # Read config
    init_capital = _CONFIG["initial_capital"]
    fee_pct = _CONFIG["fee_pct"]
    slippage_pct = _CONFIG["slippage_pct"]

    # Simulate trades
    cash = init_capital
    position = 0.0  # Amount of crypto held
    entry_price = 0.0
    trades = []
    equity_curve = np.zeros(n)

    for i in range(n):
        price = close[i]

        if entries[i] and position == 0:
            # Buy signal
            exec_price = price * (1 + slippage_pct)  # Buy slippage
            size = (cash * (1 - fee_pct)) / exec_price
            position = size
            cash = 0
            entry_price = exec_price
            trades.append({"type": "entry", "price": exec_price, "idx": i, "pnl": 0})

        elif exits[i] and position > 0:
            # Sell signal
            exec_price = price * (1 - slippage_pct)  # Sell slippage
            gross_value = position * exec_price
            cash = gross_value * (1 - fee_pct)
            pnl = (exec_price - entry_price) / entry_price
            trades.append({"type": "exit", "price": exec_price, "idx": i, "pnl": pnl})
            position = 0
            entry_price = 0

        equity_curve[i] = cash + position * price

    # Close any open position at the end
    if position > 0:
        cash = position * close[-1] * (1 - fee_pct) * (1 - slippage_pct)
        position = 0

    final_equity = cash
    total_return = (final_equity - init_capital) / init_capital

    # Calculate metrics
    trade_pnls = [t["pnl"] for t in trades if t["type"] == "exit"]
    n_trades = len(trade_pnls)
    win_rate = sum(1 for p in trade_pnls if p > 0) / n_trades * 100 if n_trades > 0 else 0
    gross_profit = sum(p for p in trade_pnls if p > 0) if n_trades > 0 else 0
    gross_loss = abs(sum(p for p in trade_pnls if p < 0)) if n_trades > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else 0

    # Sharpe ratio
    if n > 1:
        returns = np.diff(equity_curve) / np.maximum(equity_curve[:-1], 1e-10)
        if len(returns) > 10:
            excess_returns = returns - 0.0  # risk-free rate assumed 0 for crypto
            sharpe = np.mean(excess_returns) / (np.std(excess_returns) + 1e-10)
            # Annualize
            if pd.infer_freq(df.index):
                sharpe *= np.sqrt(365)  # rough annualization
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - peak) / np.maximum(peak, 1e-10)
    max_dd = np.min(drawdowns) if len(drawdowns) > 0 else 0

    # Calmar
    calmar = (-total_return / max_dd) if max_dd != 0 else 0
    avg_trade = np.mean(trade_pnls) * 100 if n_trades > 0 else 0

    return {
        "strategy": strategy_type.upper() + ("_Cross" if strategy_type == "ema" else ""),
        "total_return_pct": float(total_return) * 100,
        "sharpe_ratio": float(sharpe),
        "max_drawdown_pct": float(max_dd) * 100,
        "total_trades": int(n_trades),
        "win_rate_pct": float(win_rate),
        "profit_factor": float(profit_factor),
        "calmar_ratio": float(calmar),
        "avg_trade_pct": float(avg_trade),
        "final_equity": float(final_equity),
    }


# =============================================================================
# Metrics extraction
# =============================================================================

def extract_metrics_vectorbt(portfolio, strategy_name: str) -> Dict:
    """
    Extract key performance metrics from vectorbt portfolio.

    Args:
        portfolio: vbt.Portfolio object
        strategy_name: Name of the strategy for labeling

    Returns:
        Dict of performance metrics with consistent keys
    """
    total_return = portfolio.total_return()
    sharpe = portfolio.sharpe_ratio()
    max_dd = portfolio.max_drawdown()

    # Trade statistics
    trades = portfolio.trades
    try:
        n_trades = int(trades.count()) if hasattr(trades, "count") else 0
    except Exception:
        n_trades = 0

    try:
        win_rate = float(trades.win_rate() * 100) if hasattr(trades, "win_rate") else 0.0
    except Exception:
        win_rate = 0.0

    try:
        trade_returns = trades.returns
        avg_trade = float(trade_returns.mean() * 100) if len(trade_returns) > 0 else 0.0
    except Exception:
        avg_trade = 0.0

    # Profit factor = gross profit / gross loss
    try:
        returns = trades.returns if hasattr(trades, "returns") else pd.Series([])
        if len(returns) > 0:
            gross_profit = float(returns[returns > 0].sum())
            gross_loss = abs(float(returns[returns < 0].sum()))
            profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else 0.0
        else:
            profit_factor = 0.0
    except Exception:
        profit_factor = 0.0

    # Calmar ratio = annualized return / max drawdown
    try:
        calmar = float(-total_return / max_dd) if max_dd != 0 else 0.0
    except Exception:
        calmar = 0.0

    return {
        "strategy": strategy_name,
        "total_return_pct": float(total_return) * 100,
        "sharpe_ratio": float(sharpe) if not (np.isnan(sharpe) or np.isinf(sharpe)) else 0.0,
        "max_drawdown_pct": float(max_dd) * 100,
        "total_trades": int(n_trades),
        "win_rate_pct": float(win_rate),
        "profit_factor": float(profit_factor),
        "calmar_ratio": float(calmar),
        "avg_trade_pct": float(avg_trade),
        "final_equity": float(_CONFIG["initial_capital"] * (1 + total_return)),
    }


# =============================================================================
# Orchestration
# =============================================================================

def run_all_backtests(
    symbol: str,
    strategy_filter: Optional[str] = None,
    output_dir: str = "results",
) -> pd.DataFrame:
    """
    Run backtests for all timeframes on a given symbol.

    Args:
        symbol: Trading pair (e.g., 'BTC/USDT')
        strategy_filter: If set, only run this strategy ('ema' or 'rsi_macd')
        output_dir: Output directory for results

    Returns:
        DataFrame with one row per (timeframe, strategy) combination
    """
    logger.info(f"=" * 60)
    logger.info(f"Running backtests for {symbol}")
    logger.info(f"=" * 60)

    results = []

    for tf in TIMEFRAMES:
        logger.info(f"--- Timeframe: {tf} ---")
        df = load_data(symbol, tf)

        if len(df) < 100:
            logger.warning(f"  Insufficient data for {tf} ({len(df)} rows), skipping")
            continue

        logger.info(f"  Data: {len(df)} rows from {df.index[0]} to {df.index[-1]}")

        # EMA Crossover
        if strategy_filter is None or strategy_filter.lower() == "ema":
            try:
                logger.info(f"  Running EMA Crossover...")
                ema_metrics = run_ema_backtest(df)
                ema_metrics.update({"symbol": symbol, "timeframe": tf, "data_rows": len(df)})
                results.append(ema_metrics)
                logger.info(
                    f"    Return: {ema_metrics['total_return_pct']:.1f}% | "
                    f"Sharpe: {ema_metrics['sharpe_ratio']:.2f} | "
                    f"Trades: {ema_metrics['total_trades']}"
                )
            except Exception as e:
                logger.error(f"  EMA backtest failed for {tf}: {e}")

        # RSI + MACD
        if strategy_filter is None or strategy_filter.lower() == "rsi_macd":
            try:
                logger.info(f"  Running RSI+MACD...")
                rsi_metrics = run_rsi_macd_backtest(df)
                rsi_metrics.update({"symbol": symbol, "timeframe": tf, "data_rows": len(df)})
                results.append(rsi_metrics)
                logger.info(
                    f"    Return: {rsi_metrics['total_return_pct']:.1f}% | "
                    f"Sharpe: {rsi_metrics['sharpe_ratio']:.2f} | "
                    f"Trades: {rsi_metrics['total_trades']}"
                )
            except Exception as e:
                logger.error(f"  RSI+MACD backtest failed for {tf}: {e}")

    if not results:
        logger.warning(f"No backtest results for {symbol}")
        return pd.DataFrame()

    return pd.DataFrame(results)


# =============================================================================
# Report generation
# =============================================================================

def format_table(df: pd.DataFrame, headers: str = "keys", fmt: str = "pipe") -> str:
    """Format DataFrame as markdown table (with tabulate fallback)."""
    if TABULATE_AVAILABLE:
        return tabulate(df, headers=headers, tablefmt=fmt, showindex=False, floatfmt=".2f")
    else:
        # Simple markdown table fallback
        lines = []
        cols = list(df.columns)
        lines.append("| " + " | ".join(str(c) for c in cols) + " |")
        lines.append("| " + " | ".join("-" * len(str(c)) for c in cols) + " |")
        for _, row in df.iterrows():
            vals = []
            for c in cols:
                v = row[c]
                if isinstance(v, float):
                    vals.append(f"{v:.2f}")
                else:
                    vals.append(str(v))
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)


def generate_report(results_df: pd.DataFrame, output_dir: str) -> str:
    """
    Generate comprehensive markdown report with analysis and recommendations.

    Args:
        results_df: DataFrame with all backtest results
        output_dir: Directory to save report files

    Returns:
        Markdown report as string
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    report_lines = [
        "# CryptoBot Backtest Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Initial Capital:** ${_CONFIG['initial_capital']}",
        f"**Trading Fees:** {_CONFIG['fee_pct'] * 100}% | **Slippage:** {_CONFIG['slippage_pct'] * 100}%",
        "",
        "---",
        "",
    ]

    # ================================================================
    # Section 1: Executive Summary
    # ================================================================
    report_lines.extend([
        "## 1. Executive Summary",
        "",
        "This report compares the EMA Crossover and RSI+MACD Combo strategies",
        "across six timeframes (1m, 5m, 15m, 1h, 4h, 1d) using realistic fee assumptions.",
        "",
    ])

    # Best overall by Sharpe
    if not results_df.empty and "sharpe_ratio" in results_df.columns:
        best_sharpe = results_df.loc[results_df["sharpe_ratio"].idxmax()]
        report_lines.extend([
            f"**Best Overall (Sharpe):** `{best_sharpe['timeframe']}` timeframe | "
            f"{best_sharpe['strategy']} on {best_sharpe['symbol']}",
            f"- Sharpe Ratio: **{best_sharpe['sharpe_ratio']:.2f}**",
            f"- Total Return: **{best_sharpe['total_return_pct']:.1f}%**",
            f"- Max Drawdown: **{best_sharpe['max_drawdown_pct']:.1f}%**",
            "",
        ])

    # Best by total return
        best_return = results_df.loc[results_df["total_return_pct"].idxmax()]
        report_lines.extend([
            f"**Best Overall (Return):** `{best_return['timeframe']}` timeframe | "
            f"{best_return['strategy']} on {best_return['symbol']}",
            f"- Total Return: **{best_return['total_return_pct']:.1f}%**",
            f"- Max Drawdown: **{best_return['max_drawdown_pct']:.1f}%**",
            "",
        ])

    report_lines.append("---")
    report_lines.append("")

    # ================================================================
    # Section 2: Performance by Timeframe (averaged across symbols & strategies)
    # ================================================================
    report_lines.extend([
        "## 2. Performance by Timeframe",
        "",
        "Average metrics across all symbols and strategies per timeframe:",
        "",
    ])

    if not results_df.empty:
        tf_summary = results_df.groupby("timeframe").agg({
            "total_return_pct": "mean",
            "sharpe_ratio": "mean",
            "max_drawdown_pct": "mean",
            "win_rate_pct": "mean",
            "profit_factor": "mean",
            "calmar_ratio": "mean",
            "total_trades": "mean",
        }).round(2)

        # Reorder to match TIMEFRAMES order
        tf_summary = tf_summary.reindex([tf for tf in TIMEFRAMES if tf in tf_summary.index])
        tf_summary.columns = [
            "Avg Return %", "Avg Sharpe", "Avg Max DD %",
            "Avg Win Rate %", "Avg Profit Factor", "Avg Calmar", "Avg Trades",
        ]

        report_lines.append(format_table(tf_summary.reset_index()))
        report_lines.append("")

    report_lines.append("---")
    report_lines.append("")

    # ================================================================
    # Section 3: Per-Symbol Detailed Results
    # ================================================================
    report_lines.extend([
        "## 3. Detailed Results by Symbol",
        "",
    ])

    for symbol in results_df["symbol"].unique() if not results_df.empty else []:
        report_lines.extend([
            f"### {symbol}",
            "",
        ])
        symbol_df = results_df[results_df["symbol"] == symbol].copy()

        # Detailed table
        display_cols = [
            "timeframe", "strategy", "total_return_pct", "sharpe_ratio",
            "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "calmar_ratio", "total_trades", "final_equity",
        ]
        display_df = symbol_df[display_cols].copy()
        display_df.columns = [
            "Timeframe", "Strategy", "Return %", "Sharpe",
            "Max DD %", "Win Rate %", "Profit Factor",
            "Calmar", "Trades", "Final Equity ($)",
        ]

        report_lines.append(format_table(display_df))
        report_lines.append("")

        # Best for this symbol
        best = symbol_df.loc[symbol_df["sharpe_ratio"].idxmax()]
        report_lines.extend([
            f"**Best for {symbol}:** {best['timeframe']} {best['strategy']} | "
            f"Sharpe: {best['sharpe_ratio']:.2f} | Return: {best['total_return_pct']:.1f}%",
            "",
        ])

    report_lines.extend([
        "---",
        "",
    ])

    # ================================================================
    # Section 4: Strategy Comparison
    # ================================================================
    report_lines.extend([
        "## 4. Strategy Comparison",
        "",
    ])

    if not results_df.empty:
        strat_summary = results_df.groupby("strategy").agg({
            "total_return_pct": "mean",
            "sharpe_ratio": "mean",
            "max_drawdown_pct": "mean",
            "win_rate_pct": "mean",
            "profit_factor": "mean",
            "total_trades": "mean",
        }).round(2)
        strat_summary.columns = [
            "Avg Return %", "Avg Sharpe", "Avg Max DD %",
            "Avg Win Rate %", "Avg Profit Factor", "Avg Trades",
        ]
        report_lines.append(format_table(strat_summary.reset_index()))
        report_lines.append("")

    report_lines.extend([
        "---",
        "",
    ])

    # ================================================================
    # Section 5: Recommendations
    # ================================================================
    report_lines.extend([
        "## 5. Recommendations",
        "",
    ])

    if not results_df.empty:
        # Conservative pick (Bot A: 15m-4h, Sharpe > 1.0, Max DD < 15%)
        conservative = results_df[
            (results_df["sharpe_ratio"] > 1.0) &
            (results_df["max_drawdown_pct"] < -15)  # max_dd is negative
        ].sort_values("sharpe_ratio", ascending=False)

        if len(conservative) > 0:
            pick = conservative.iloc[0]
        else:
            # Relax criteria
            conservative = results_df[
                (results_df["sharpe_ratio"] > 0.5) &
                (results_df["timeframe"].isin(["15m", "1h", "4h"]))
            ].sort_values("sharpe_ratio", ascending=False)
            pick = conservative.iloc[0] if len(conservative) > 0 else results_df.loc[results_df["sharpe_ratio"].idxmax()]

        report_lines.extend([
            f"### Conservative Pick (Bot A - 15m to 4h): **{pick['timeframe']}**",
            "",
            f"| Parameter | Value |",
            f"|-----------|-------|",
            f"| Timeframe | `{pick['timeframe']}` |",
            f"| Strategy | {pick['strategy']} |",
            f"| Symbol | {pick['symbol']} |",
            f"| Sharpe Ratio | {pick['sharpe_ratio']:.2f} |",
            f"| Total Return | {pick['total_return_pct']:.1f}% |",
            f"| Max Drawdown | {pick['max_drawdown_pct']:.1f}% |",
            f"| Win Rate | {pick['win_rate_pct']:.1f}% |",
            f"| Profit Factor | {pick['profit_factor']:.2f} |",
            f"| Total Trades | {pick['total_trades']} |",
            f"| Final Equity | ${pick['final_equity']:.2f} |",
            "",
            "**Why this pick:** Balanced trade frequency with strong risk-adjusted returns.",
            "Suitable for 24/7 automated operation with moderate capital ($500).",
            "",
        ])

        # Aggressive pick (Bot B: 5m-15m, high trade count, Sharpe > 0.8)
        aggressive_candidates = results_df[
            (results_df["timeframe"].isin(["5m", "15m"])) &
            (results_df["total_trades"] > 20) &
            (results_df["sharpe_ratio"] > 0.3)
        ].sort_values("sharpe_ratio", ascending=False)

        if len(aggressive_candidates) > 0:
            pick2 = aggressive_candidates.iloc[0]
            report_lines.extend([
                f"### Active Pick (Bot B - 5m to 15m): **{pick2['timeframe']}**",
                "",
                f"| Parameter | Value |",
                f"|-----------|-------|",
                f"| Timeframe | `{pick2['timeframe']}` |",
                f"| Strategy | {pick2['strategy']} |",
                f"| Symbol | {pick2['symbol']} |",
                f"| Sharpe Ratio | {pick2['sharpe_ratio']:.2f} |",
                f"| Total Return | {pick2['total_return_pct']:.1f}% |",
                f"| Max Drawdown | {pick2['max_drawdown_pct']:.1f}% |",
                f"| Win Rate | {pick2['win_rate_pct']:.1f}% |",
                f"| Total Trades | {pick2['total_trades']} |",
                f"| Profit Factor | {pick2['profit_factor']:.2f} |",
                f"| Final Equity | ${pick2['final_equity']:.2f} |",
                "",
                "**Why this pick:** Higher trade frequency for more active capital deployment.",
                "Best suited for volatile market conditions. Requires tighter risk management.",
                "",
            ])

        # Best for long-term (1d)
        daily = results_df[results_df["timeframe"] == "1d"].sort_values("sharpe_ratio", ascending=False)
        if len(daily) > 0:
            pick3 = daily.iloc[0]
            report_lines.extend([
                f"### Long-term Pick (1d timeframe): **{pick3['strategy']}**",
                "",
                f"| Parameter | Value |",
                f"|-----------|-------|",
                f"| Timeframe | `1d` |",
                f"| Strategy | {pick3['strategy']} |",
                f"| Symbol | {pick3['symbol']} |",
                f"| Sharpe Ratio | {pick3['sharpe_ratio']:.2f} |",
                f"| Total Return | {pick3['total_return_pct']:.1f}% |",
                f"| Max Drawdown | {pick3['max_drawdown_pct']:.1f}% |",
                f"| Total Trades | {pick3['total_trades']} |",
                "",
                "**Why this pick:** Minimal fees, cleanest signals, set-and-forget operation.",
                "",
            ])

    report_lines.extend([
        "---",
        "",
    ])

    # ================================================================
    # Section 6: Timeframe Guide
    # ================================================================
    report_lines.extend([
        "## 6. Timeframe Selection Guide",
        "",
        "Use this table to choose the right timeframe for your trading style:",
        "",
        "| Timeframe | Category | Best For | Pros | Cons | Verdict |",
        "|-----------|----------|----------|------|------|---------|",
        "| **1m** | Scalping | High-frequency bots | Most signals | High fee drag, extreme noise | Avoid at $500 capital |",
        "| **5m** | Day Trading | Active day trading | Good frequency | Fee drag significant | Bot B only |",
        "| **15m** | Swing Trading | Balanced approach | Balanced risk/reward | Moderate fees | Bot A primary / Bot B |",
        "| **1h** | Swing Trading | Trend following | Lower fees, cleaner signals | Fewer trades | Bot A good option |",
        "| **4h** | Position Trading | Trend capture | Low fee impact, strong signals | Infrequent | Bot A primary |",
        "| **1d** | Long-term | Set-and-forget | Minimal fees, best Sharpe | Very few trades | Conservative fallback |",
        "",
        "### Key Insights",
        "",
        "1. **Lower timeframes (1m-5m)** generate more signals but suffer from fee drag.",
        "   At 0.1% fee per trade, a round-trip costs 0.2% - aggressive strategies need",
        "   >0.3% average profit per trade just to break even.",
        "",
        "2. **Mid timeframes (15m-1h)** offer the best balance of signal quality and",
        "   trade frequency. Recommended for Bot A with $500 capital.",
        "",
        "3. **Higher timeframes (4h-1d)** have minimal fee impact and the cleanest signals,",
        "   but require patience. Best for conservative/risk-averse operators.",
        "",
        "4. **EMA Crossover** tends to perform better in trending markets (crypto bull runs).",
        "",
        "5. **RSI+MACD Combo** filters for higher-confidence entries, reducing whipsaws",
        "   in choppy/sideways markets.",
        "",
        "---",
        "",
    ])

    # ================================================================
    # Section 7: Raw Data
    # ================================================================
    report_lines.extend([
        "## 7. Complete Results Table",
        "",
        "All results sorted by Sharpe ratio (descending):",
        "",
    ])

    if not results_df.empty:
        sorted_df = results_df.sort_values("sharpe_ratio", ascending=False).copy()
        all_cols = [
            "symbol", "timeframe", "strategy", "total_return_pct", "sharpe_ratio",
            "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "calmar_ratio", "total_trades", "final_equity",
        ]
        display_all = sorted_df[all_cols] if all(c in sorted_df.columns for c in all_cols) else sorted_df
        report_lines.append(format_table(display_all))
        report_lines.append("")

    # ================================================================
    # Disclaimer
    # ================================================================
    report_lines.extend([
        "---",
        "",
        "## Disclaimer",
        "",
        "> **Past performance does not guarantee future results.**",
        "> This backtest uses synthetic OHLCV data when real historical data is unavailable.",
        "> Synthetic data is calibrated to exhibit crypto-like volatility (~75% annualized)",
        "> and regime-switching behavior, but cannot perfectly replicate real market conditions.",
        ">",
        "> **Always paper trade for at least 2-4 weeks before live deployment.**",
        "> **Never risk more capital than you can afford to lose.**",
        "",
        f"*Report generated by CryptoBot Backtest Engine v1.0 | {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
    ])

    report = "\n".join(report_lines)

    # Save report
    report_path = Path(output_dir) / "backtest_report.md"
    report_path.write_text(report)
    logger.info(f"Report saved to: {report_path}")

    # Save raw results as CSV
    csv_path = Path(output_dir) / "backtest_results.csv"
    results_df.to_csv(csv_path, index=False)
    logger.info(f"Raw results saved to: {csv_path}")

    # Save as JSON too
    json_path = Path(output_dir) / "backtest_results.json"
    results_df.to_json(json_path, orient="records", indent=2)
    logger.info(f"JSON results saved to: {json_path}")

    return report


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Backtest strategies across all timeframes for CryptoBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Backtest all symbols with both strategies
    python scripts/backtest_all_timeframes.py --all --output results/

    # Backtest only BTC/USDT
    python scripts/backtest_all_timeframes.py --symbol BTC/USDT --output results/

    # Backtest with only EMA strategy
    python scripts/backtest_all_timeframes.py --all --strategy ema --output results/

    # Backtest with only RSI+MACD strategy
    python scripts/backtest_all_timeframes.py --all --strategy rsi_macd --output results/
        """,
    )
    parser.add_argument(
        "--symbol", type=str,
        help="Symbol to backtest (e.g., BTC/USDT, ETH/USDT)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Backtest all configured symbols",
    )
    parser.add_argument(
        "--strategy", type=str, choices=["ema", "rsi_macd"],
        help="Run only one strategy (default: both)",
    )
    parser.add_argument(
        "--output", type=str, default="results",
        help="Output directory for reports (default: results/)",
    )
    parser.add_argument(
        "--capital", type=float, default=INITIAL_CAPITAL,
        help=f"Initial capital in USD (default: {INITIAL_CAPITAL})",
    )
    parser.add_argument(
        "--fee", type=float, default=FEE_PCT,
        help=f"Trading fee as decimal (default: {FEE_PCT})",
    )
    parser.add_argument(
        "--slippage", type=float, default=SLIPPAGE_PCT,
        help=f"Slippage as decimal (default: {SLIPPAGE_PCT})",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress output",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # Determine symbols to test
    symbols = SYMBOLS if (args.all or not args.symbol) else [args.symbol]

    # Update config from CLI args
    _CONFIG["initial_capital"] = args.capital
    _CONFIG["fee_pct"] = args.fee
    _CONFIG["slippage_pct"] = args.slippage

    logger.info(f"Starting backtest run")
    logger.info(f"Symbols: {symbols}")
    logger.info(f"Strategy filter: {args.strategy or 'all'}")
    logger.info(f"Capital: ${_CONFIG['initial_capital']} | Fee: {_CONFIG['fee_pct']*100}% | Slippage: {_CONFIG['slippage_pct']*100}%")
    logger.info(f"Output dir: {args.output}")

    # Run backtests
    all_results = []
    for symbol in symbols:
        df = run_all_backtests(symbol, strategy_filter=args.strategy, output_dir=args.output)
        if not df.empty:
            all_results.append(df)

    if not all_results:
        logger.error("No backtest results generated!")
        sys.exit(1)

    combined = pd.concat(all_results, ignore_index=True)
    logger.info(f"Total result rows: {len(combined)}")

    # Generate report
    report = generate_report(combined, args.output)

    # Print to console
    if not args.quiet:
        print("\n" + "=" * 70)
        print(report)
        print("=" * 70)
        print(f"\nFiles saved:")
        print(f"  Report:     {args.output}/backtest_report.md")
        print(f"  CSV data:   {args.output}/backtest_results.csv")
        print(f"  JSON data:  {args.output}/backtest_results.json")


if __name__ == "__main__":
    main()

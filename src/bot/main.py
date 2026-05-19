#!/usr/bin/env python3
"""
Trading bot entrypoint.

Parses command-line arguments, loads configuration, initialises the
:class:`BotRunner`, and runs until a shutdown signal is received.

Usage::

    # Paper mode with config file
    python -m src.bot.main --config config/bot_a.yaml

    # Paper mode with CLI overrides
    python -m src.bot.main --bot-id bot_a --symbols BTC/USDT --timeframe 1d

    # Live mode (requires API keys)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy python -m src.bot.main --live

Environment Variables
---------------------
BINANCE_API_KEY :
    Binance API key (required for live trading).
BINANCE_API_SECRET :
    Binance API secret (required for live trading).
BINANCE_TESTNET :
    Set to ``"0"`` or ``"false"`` to use live Binance API instead of testnet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------- #
# Project root setup — add project root and src/ to import path
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot.bot_runner import BotRunner
from bot.config_loader import ConfigLoader


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(bot_id)s] %(name)s — %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_DEFAULT_LOG_LEVEL = logging.INFO


# --------------------------------------------------------------------------- #
# Structured logging filter — injects *bot_id* into every record
# --------------------------------------------------------------------------- #

class BotContextFilter(logging.Filter):
    """Logging filter that adds ``bot_id`` to every LogRecord.

    This ensures every log line carries the bot identifier for easy
    filtering in aggregated logging systems.
    """

    def __init__(self, bot_id: str = "unknown") -> None:
        super().__init__()
        self.bot_id = bot_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.bot_id = self.bot_id  # type: ignore[attr-defined]
        return True


def setup_logging(
    bot_id: str,
    level: int = _DEFAULT_LOG_LEVEL,
    log_file: Optional[str] = None,
) -> None:
    """Configure structured logging with *bot_id* in every line.

    Parameters
    ----------
    bot_id :
        Bot identifier injected into every log record.
    level :
        Python logging level (default ``logging.INFO``).
    log_file :
        Optional file path for rotating log output.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
            )
        )

    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATE_FORMAT,
        handlers=handlers,
        force=True,
    )

    # Inject bot_id into all log records
    context_filter = BotContextFilter(bot_id=bot_id)
    root_logger = logging.getLogger()
    root_logger.addFilter(context_filter)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(
        "[%s] Logging initialised — level=%s file=%s",
        bot_id,
        logging.getLevelName(level),
        log_file,
    )


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Crypto Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.bot.main --config config/bot_a.yaml\n"
            "  python -m src.bot.main --bot-id bot_a --symbols BTC/USDT ETH/USDT\n"
            "  BINANCE_API_KEY=xxx python -m src.bot.main --live --timeframe 1h\n"
        ),
    )

    parser.add_argument(
        "--config",
        help="Path to bot config YAML file",
    )
    parser.add_argument(
        "--bot-id",
        default="bot_a",
        help="Bot identifier (default: bot_a)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC/USDT"],
        help="Trading symbols (default: BTC/USDT)",
    )
    parser.add_argument(
        "--timeframe",
        default="1d",
        help="Candle timeframe, e.g. 1m, 5m, 15m, 1h, 4h, 1d (default: 1d)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        default=True,
        help="Run in paper trading mode (default)",
    )
    parser.add_argument(
        "--live",
        action="store_false",
        dest="paper",
        help="Run in live trading mode (requires API keys)",
    )
    parser.add_argument(
        "--db-path",
        default="data/cryptobot.duckdb",
        help="Path to DuckDB database file (default: data/cryptobot.duckdb)",
    )
    parser.add_argument(
        "--log-file",
        help="Path to rotating log file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG level logging",
    )
    parser.add_argument(
        "--risk-profile",
        default="conservative",
        help="Risk profile: conservative, moderate, aggressive (default: conservative)",
    )
    parser.add_argument(
        "--strategy",
        default="ema_crossover",
        help="Strategy: ema_crossover, rsi_macd (default: ema_crossover)",
    )

    return parser


# --------------------------------------------------------------------------- #
# Config assembly
# --------------------------------------------------------------------------- #

def assemble_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Build the merged configuration dictionary from all sources.

    Priority (highest first):
        1. Command-line arguments
        2. Config file
        3. Hardcoded defaults

    Parameters
    ----------
    args :
        Parsed CLI arguments.

    Returns
    -------
    dict
        Complete configuration dictionary.
    """
    loader = ConfigLoader()

    # Base: load from file if provided, else defaults
    if args.config:
        config = loader.load_bot_config(args.bot_id)
    else:
        config = loader.get_bot_config_defaults()
        config["bot_id"] = args.bot_id

    # Overlay CLI arguments
    config["symbols"] = args.symbols
    config["timeframe"] = args.timeframe
    config["paper"] = args.paper
    config["db_path"] = args.db_path
    config["risk_profile"] = args.risk_profile
    config["strategy"] = args.strategy

    # Load risk config
    risk_cfg = loader.load_risk_config(args.risk_profile)
    config["risk"] = risk_cfg

    # Load exchange config (API keys from env)
    exchange_cfg = loader.load_exchange_config()
    config["exchange"] = exchange_cfg

    # Validate
    if not loader.validate_config(config):
        raise ValueError("Configuration validation failed")

    return config


# --------------------------------------------------------------------------- #
# Main coroutine
# --------------------------------------------------------------------------- #

async def main() -> int:
    """Async entrypoint.

    Returns
    -------
    int
        Process exit code (0 = success, 1 = error).
    """
    parser = build_parser()
    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else _DEFAULT_LOG_LEVEL
    setup_logging(
        bot_id=args.bot_id,
        level=log_level,
        log_file=args.log_file,
    )
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Crypto Trading Bot — starting up")
    logger.info("=" * 60)

    # Load and merge configuration
    try:
        config = assemble_config(args)
    except ValueError as exc:
        logger.error("Failed to load configuration: %s", exc)
        return 1

    logger.info(
        "[%s] Config loaded — symbol=%s timeframe=%s paper=%s "
        "strategy=%s risk_profile=%s",
        config["bot_id"],
        config["symbols"][0],
        config["timeframe"],
        config["paper"],
        config["strategy"],
        config["risk_profile"],
    )

    # Create and initialise runner
    runner: Optional[BotRunner] = None
    try:
        runner = BotRunner(config)
        await runner.initialize()
    except Exception as exc:
        logger.critical(
            "[%s] Initialisation failed: %s",
            config["bot_id"],
            exc,
            exc_info=True,
        )
        return 1

    # Register signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runner.signal_handler, sig)
            logger.debug(
                "[%s] Registered handler for %s",
                config["bot_id"],
                sig.name,
            )
        except (ValueError, OSError, NotImplementedError):
            # Windows does not support add_signal_handler for all signals
            logger.warning(
                "[%s] Could not register signal handler for %s",
                config["bot_id"],
                sig.name,
            )

    # Run until shutdown
    exit_code = 0
    try:
        logger.info("[%s] Entering main trading loop …", config["bot_id"])
        await runner.run()
    except asyncio.CancelledError:
        logger.info("[%s] Main task cancelled", config["bot_id"])
    except Exception as exc:
        logger.critical(
            "[%s] Unhandled exception in main loop: %s",
            config["bot_id"],
            exc,
            exc_info=True,
        )
        exit_code = 1
    finally:
        # Ensure clean shutdown
        if runner is not None:
            try:
                await runner.shutdown()
            except Exception as exc:
                logger.error(
                    "[%s] Error during shutdown: %s",
                    config["bot_id"],
                    exc,
                )

    logger.info("[%s] Bot exited — code=%d", config["bot_id"], exit_code)
    return exit_code


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        # KeyboardInterrupt is expected during development / Ctrl-C
        logging.getLogger(__name__).info("Interrupted by user (KeyboardInterrupt)")
        sys.exit(0)
    except Exception as exc:
        logging.getLogger(__name__).critical(
            "Fatal error during startup: %s", exc, exc_info=True
        )
        sys.exit(1)

"""Structured JSON logging for Loki ingestion.

Configures:
- JSON formatter for machine-readable logs
- Log rotation (10MB files, 5 backups)
- Per-module log levels
- bot_id injection into every log record
- Separate audit log for compliance

Usage:
    from bot.structured_logging import setup_logging

    setup_logging(bot_id="bot_a", log_dir="logs", level="INFO")

    logger = logging.getLogger(__name__)
    logger.info("Bot started", extra={"event": "bot_start", "capital": 500})
"""

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class JSONFormatter(logging.Formatter):
    """Format log records as JSON."""

    def __init__(self, bot_id: str = "unknown"):
        self.bot_id = bot_id
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "bot_id": self.bot_id,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields
        if hasattr(record, "event"):
            log_data["event"] = record.event
        if hasattr(record, "symbol"):
            log_data["symbol"] = record.symbol
        if hasattr(record, "trade_id"):
            log_data["trade_id"] = record.trade_id

        # Add exception info
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


class BotContextFilter(logging.Filter):
    """Inject bot_id into every log record."""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        super().__init__()

    def filter(self, record: logging.LogRecord) -> bool:
        record.bot_id = self.bot_id
        return True


def setup_logging(bot_id: str, log_dir: str = "logs", level: str = "INFO"):
    """Setup structured JSON logging.

    Args:
        bot_id: Bot identifier injected into all logs
        log_dir: Directory for log files
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # JSON handler (rotating file)
    json_handler = logging.handlers.RotatingFileHandler(
        log_path / f"{bot_id}.json.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    json_handler.setFormatter(JSONFormatter(bot_id))
    json_handler.addFilter(BotContextFilter(bot_id))
    root_logger.addHandler(json_handler)

    # Console handler (human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_format = logging.Formatter(
        "%(asctime)s [%(bot_id)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_format)
    console_handler.addFilter(BotContextFilter(bot_id))
    root_logger.addHandler(console_handler)

    # Audit log (separate file for compliance)
    audit_handler = logging.handlers.RotatingFileHandler(
        log_path / f"{bot_id}.audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
    )
    audit_handler.setFormatter(JSONFormatter(bot_id))
    audit_handler.addFilter(BotContextFilter(bot_id))
    audit_logger = logging.getLogger("audit")
    audit_logger.addHandler(audit_handler)
    audit_logger.setLevel(logging.INFO)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured — bot_id={bot_id}, level={level}, dir={log_dir}")

"""
Config loader — reads and validates YAML configuration files.

Searches the ``config/`` directory (relative to project root) for bot-specific,
risk, exchange, and alert configurations.  Provides hardcoded defaults so the
bot can start even when a config file is missing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_BOT_REQUIRED_FIELDS: Set[str] = {
    "bot_id",
    "symbols",
    "timeframe",
    "risk_profile",
}

_RISK_REQUIRED_FIELDS: Set[str] = {
    "per_trade_risk_pct",
    "max_drawdown_kill_pct",
    "daily_cap_pct",
    "max_position_pct",
}

_DEFAULT_BOT_CONFIG: Dict[str, Any] = {
    "bot_id": "bot_a",
    "symbols": ["BTC/USDT", "ETH/USDT"],
    "timeframe": "1d",
    "initial_capital_usd": 500,
    "risk_profile": "conservative",
    "paper": True,
    "strategy": "ema_crossover",
    "strategy_params": {},
    "heartbeat_interval_sec": 30,
    "reconciliation_interval_sec": 60,
    "db_path": "data/cryptobot.duckdb",
}

_DEFAULT_RISK_CONFIG: Dict[str, Any] = {
    "per_trade_risk_pct": 1.0,
    "max_drawdown_kill_pct": 10.0,
    "daily_cap_pct": 3.0,
    "weekly_cap_pct": 5.0,
    "monthly_cap_pct": 8.0,
    "max_position_pct": 50.0,
    "signal_strength_threshold": 0.3,
}


# --------------------------------------------------------------------------- #
# ConfigLoader
# --------------------------------------------------------------------------- #

class ConfigLoader:
    """Load and validate YAML configuration files from a directory tree.

    Parameters
    ----------
    config_dir :
        Path to the directory containing ``bot_*.yaml``, ``risk_*.yaml``,
        ``exchange.yaml`` and ``alerts.yaml``.
    """

    def __init__(self, config_dir: str = "config") -> None:
        self.config_dir = Path(config_dir)
        if not self.config_dir.is_absolute():
            # Resolve relative to project root (two levels above src/bot/)
            project_root = Path(__file__).parent.parent.parent
            self.config_dir = project_root / self.config_dir
        logger.debug(
            "ConfigLoader initialised — config_dir=%s",
            self.config_dir.resolve(),
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def load_bot_config(self, bot_id: str) -> Dict[str, Any]:
        """Load bot configuration for *bot_id*.

        Searches for ``bot_<bot_id>.yaml`` or ``<bot_id>.yaml`` in
        *config_dir*.  Falls back to :meth:`get_bot_config_defaults` if the
        file is missing or YAML is unavailable.

        Parameters
        ----------
        bot_id :
            Bot identifier, e.g. ``"bot_a"``.

        Returns
        -------
        dict
            Merged configuration dict.
        """
        candidates = [
            self.config_dir / f"bot_{bot_id}.yaml",
            self.config_dir / f"{bot_id}.yaml",
            self.config_dir / f"{bot_id}.yml",
        ]

        for path in candidates:
            config = self._load_yaml(path)
            if config is not None:
                logger.info("Loaded bot config from %s", path)
                # Merge with defaults so missing keys are backfilled
                merged = self.get_bot_config_defaults()
                merged.update(config)
                merged["bot_id"] = bot_id
                return merged

        logger.warning(
            "No bot config file found for %s — using hardcoded defaults",
            bot_id,
        )
        defaults = self.get_bot_config_defaults()
        defaults["bot_id"] = bot_id
        return defaults

    def load_risk_config(self, profile: str = "conservative") -> Dict[str, Any]:
        """Load risk configuration for *profile*.

        Parameters
        ----------
        profile :
            Risk profile name, e.g. ``"conservative"``, ``"moderate"``,
            ``"aggressive"``.

        Returns
        -------
        dict
            Risk parameter dictionary.
        """
        candidates = [
            self.config_dir / f"risk_{profile}.yaml",
            self.config_dir / f"risk_{profile}.yml",
            self.config_dir / "risk.yaml",
            self.config_dir / "risk.yml",
        ]

        for path in candidates:
            config = self._load_yaml(path)
            if config is not None:
                logger.info("Loaded risk config from %s", path)
                merged = _DEFAULT_RISK_CONFIG.copy()
                merged.update(config)
                return merged

        logger.warning(
            "No risk config found for profile %s — using defaults",
            profile,
        )
        return _DEFAULT_RISK_CONFIG.copy()

    def load_exchange_config(self) -> Dict[str, Any]:
        """Load exchange configuration.

        Returns
        -------
        dict
            Exchange settings (API keys are read from environment variables
            for security — never commit them to YAML).
        """
        candidates = [
            self.config_dir / "exchange.yaml",
            self.config_dir / "exchange.yml",
        ]

        config: Dict[str, Any] = {}
        for path in candidates:
            loaded = self._load_yaml(path)
            if loaded is not None:
                config.update(loaded)
                logger.info("Loaded exchange config from %s", path)
                break

        # Always overlay environment variables (highest priority)
        config["api_key"] = os.environ.get("BINANCE_API_KEY", config.get("api_key", ""))
        config["api_secret"] = os.environ.get(
            "BINANCE_API_SECRET", config.get("api_secret", "")
        )
        config["testnet"] = self._env_bool(
            "BINANCE_TESTNET", config.get("testnet", True)
        )

        return config

    def load_alerts_config(self) -> Dict[str, Any]:
        """Load alerts / notification configuration.

        Returns
        -------
        dict
            Alert channel settings (webhook URLs, SMTP, etc.).
        """
        candidates = [
            self.config_dir / "alerts.yaml",
            self.config_dir / "alerts.yml",
        ]

        for path in candidates:
            config = self._load_yaml(path)
            if config is not None:
                logger.info("Loaded alerts config from %s", path)
                return config

        logger.debug("No alerts config found — notifications disabled")
        return {"enabled": False}

    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate that *config* contains all required fields.

        Parameters
        ----------
        config :
            Bot configuration dictionary.

        Returns
        -------
        bool
            ``True`` if all required fields are present and non-empty.
        """
        missing = _BOT_REQUIRED_FIELDS - set(config.keys())
        if missing:
            logger.error("Config validation failed — missing fields: %s", missing)
            return False

        if not config.get("symbols"):
            logger.error("Config validation failed — symbols list is empty")
            return False

        if not isinstance(config.get("paper"), bool):
            logger.error("Config validation failed — paper must be a boolean")
            return False

        logger.info("Config validation passed")
        return True

    # ------------------------------------------------------------------ #
    # Defaults
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_bot_config_defaults() -> Dict[str, Any]:
        """Return hardcoded default bot configuration.

        Used when a config file is missing or incomplete.

        Returns
        -------
        dict
            Complete default configuration dictionary.
        """
        return _DEFAULT_BOT_CONFIG.copy()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _load_yaml(self, path: Path) -> Optional[Dict[str, Any]]:
        """Safely load a YAML file.  Returns ``None`` on any error."""
        if not YAML_AVAILABLE:
            logger.warning("PyYAML not installed — cannot load %s", path)
            return None
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
                if isinstance(data, dict):
                    return data
                logger.warning("YAML file %s did not contain a mapping", path)
                return None
        except Exception as exc:
            logger.error("Failed to load %s: %s", path, exc)
            return None

    @staticmethod
    def _env_bool(var_name: str, default: bool = False) -> bool:
        """Read a boolean from an environment variable."""
        val = os.environ.get(var_name, "")
        if val.lower() in ("1", "true", "yes", "on"):
            return True
        if val.lower() in ("0", "false", "no", "off"):
            return False
        return default

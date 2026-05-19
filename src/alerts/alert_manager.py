"""Alert manager: reads config/alerts.yaml and routes alerts to appropriate channels.

Usage:
    from alerts.alert_manager import AlertManager

    alerts = AlertManager.from_config("config/alerts.yaml")
    await alerts.initialize()
    await alerts.send("CRITICAL", "Kill switch triggered", {"bot_id": "bot_a"})
    await alerts.send("ERROR", "Daily limit breached", {"daily_pnl": -8.5})

Severity levels: INFO, WARNING, ERROR, CRITICAL
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import yaml

from .throttle import AlertThrottle

logger = logging.getLogger(__name__)

# Severity -> default channel mapping when not overridden in YAML.
_DEFAULT_SEVERITY_MAP = {
    "CRITICAL": ["slack", "discord", "email"],
    "ERROR": ["slack", "discord"],
    "WARNING": ["slack"],
    "INFO": ["slack"],
}


class AlertManager:
    """Central alert routing manager.

    Reads ``config/alerts.yaml`` and routes by severity level.
    Supports: Slack, Discord, Email (SMTP).
    """

    def __init__(self, config: dict):
        self.config = config
        self._slack = None
        self._discord = None
        self._email = None
        self._throttle = AlertThrottle()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, path: str = "config/alerts.yaml") -> "AlertManager":
        """Load from YAML config file.

        Args:
            path: Filesystem path to the YAML configuration file.

        Returns:
            An ``AlertManager`` instance with configuration loaded but senders
            not yet initialised (call ``initialize()`` next).
        """
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        return cls(config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self):
        """Initialise all enabled senders declared in the configuration."""
        channels = self.config.get("alert_channels", {})

        # Slack
        slack_cfg = channels.get("slack", {})
        if slack_cfg.get("enabled"):
            from .slack_sender import SlackSender

            self._slack = SlackSender(slack_cfg["webhook_url"])

        # Discord
        discord_cfg = channels.get("discord", {})
        if discord_cfg.get("enabled"):
            from .discord_sender import DiscordSender

            self._discord = DiscordSender(discord_cfg["webhook_url"])

        # Email
        email_cfg = channels.get("email", {})
        if email_cfg.get("enabled"):
            from .email_sender import EmailSender

            self._email = EmailSender(
                smtp_host=email_cfg["smtp_host"],
                smtp_port=email_cfg["smtp_port"],
                username=email_cfg["username"],
                password=email_cfg["password"],
                to_address=email_cfg["to"],
            )

    async def shutdown(self):
        """Gracefully close all underlying HTTP clients."""
        if self._slack:
            await self._slack.close()
        if self._discord:
            await self._discord.close()
        # EmailSender uses synchronous smtplib; nothing to close.

    # ------------------------------------------------------------------
    # Core send API
    # ------------------------------------------------------------------

    async def send(
        self,
        severity: str,
        title: str,
        details: Optional[dict] = None,
    ):
        """Send alert to all channels configured for this severity.

        Args:
            severity: One of ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
            title: Short alert headline.
            details: Optional key/value context attached to the alert body.
        """
        severity = severity.upper()

        # Throttle check
        if not self._throttle.should_send(severity, title):
            logger.debug("Alert throttled: [%s] %s", severity, title)
            return

        # Build payload
        message = self._format_message(severity, title, details)
        channels = self._get_channels_for_severity(severity)

        # Dispatch concurrently
        tasks: List[asyncio.Task] = []
        if "slack" in channels and self._slack:
            tasks.append(asyncio.create_task(self._slack.send(message)))
        if "discord" in channels and self._discord:
            tasks.append(asyncio.create_task(self._discord.send(message)))
        if "email" in channels and self._email:
            tasks.append(asyncio.create_task(self._email.send(title, message)))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Alert channel failed: %s", result)
        else:
            logger.warning(
                "No channels configured for severity %s: %s", severity, title
            )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def send_kill_switch(self, bot_id: str, reason: str):
        """Convenience wrapper: kill-switch triggered."""
        await self.send(
            "CRITICAL",
            f"KILL SWITCH TRIGGERED — {bot_id}",
            {
                "bot_id": bot_id,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def send_reconciliation_drift(self, bot_id: str, drift: float):
        """Convenience wrapper: reconciliation drift detected."""
        await self.send(
            "WARNING",
            f"Reconciliation Drift — {bot_id}",
            {
                "bot_id": bot_id,
                "drift_usd": drift,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def send_daily_limit(self, bot_id: str, daily_pnl: float):
        """Convenience wrapper: daily limit breached."""
        await self.send(
            "ERROR",
            f"Daily Limit Breached — {bot_id}",
            {
                "bot_id": bot_id,
                "daily_pnl": daily_pnl,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_message(
        self, severity: str, title: str, details: Optional[dict]
    ) -> str:
        """Format a plain-text alert payload."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"[{severity}] {title}", f"Time: {ts}"]
        if details:
            for key, value in details.items():
                lines.append(f"{key}: {value}")
        return "\n".join(lines)

    def _get_channels_for_severity(self, severity: str) -> List[str]:
        """Resolve channels for *severity* from configuration."""
        routing = self.config.get("severity_routing", {})
        channels = routing.get(severity)
        if channels is not None:
            return channels
        return _DEFAULT_SEVERITY_MAP.get(severity, ["slack"])

"""Slack webhook alert sender."""

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


class SlackSender:
    """Sends alert messages to a Slack incoming-webhook URL.

    Example:
        sender = SlackSender("https://hooks.slack.com/services/...")
        await sender.send("[CRITICAL] Kill switch triggered")
        await sender.close()
    """

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.client = httpx.AsyncClient(timeout=10.0)

    async def send(self, message: str):
        """Send message to Slack webhook.

        Args:
            message: Plain-text payload posted to the webhook.
        """
        try:
            payload = {
                "text": message,
                "username": "CryptoBot Alert",
                "icon_emoji": ":warning:",
            }
            resp = await self.client.post(self.webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("Slack alert sent")
        except Exception:
            logger.exception("Failed to send Slack alert")

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()

"""Discord webhook alert sender."""

import logging

import httpx

logger = logging.getLogger(__name__)


class DiscordSender:
    """Sends alert messages to a Discord incoming-webhook URL.

    Example:
        sender = DiscordSender("https://discord.com/api/webhooks/...")
        await sender.send("[CRITICAL] Kill switch triggered")
        await sender.close()
    """

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.client = httpx.AsyncClient(timeout=10.0)

    async def send(self, message: str):
        """Send message to Discord webhook.

        Args:
            message: Plain-text content posted to the webhook.
        """
        try:
            payload = {"content": message, "username": "CryptoBot Alert"}
            resp = await self.client.post(self.webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("Discord alert sent")
        except Exception:
            logger.exception("Failed to send Discord alert")

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()

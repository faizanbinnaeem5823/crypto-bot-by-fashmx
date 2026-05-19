"""SMTP email alert sender."""

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class EmailSender:
    """Sends alert messages via SMTP with STARTTLS.

    Example:
        sender = EmailSender(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="bot@example.com",
            password="app_password",
            to_address="ops@example.com",
        )
        await sender.send("CRITICAL: Kill switch triggered", "Body text...")
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to_address: str,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to_address = to_address

    async def send(self, subject: str, body: str):
        """Send email via SMTP.

        Args:
            subject: Email subject line (prefixed with '[CryptoBot]').
            body: Plain-text email body.
        """
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls(context=context)
                server.login(self.username, self.password)

                msg = MIMEText(body)
                msg["Subject"] = f"[CryptoBot] {subject}"
                msg["From"] = self.username
                msg["To"] = self.to_address
                msg["Date"] = datetime.now(timezone.utc).strftime(
                    "%a, %d %b %Y %H:%M:%S +0000"
                )

                server.send_message(msg)
                logger.info("Email alert sent to %s", self.to_address)
        except Exception:
            logger.exception("Failed to send email alert")

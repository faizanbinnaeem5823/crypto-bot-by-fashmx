"""Alert throttling: prevents alert storms and notification fatigue.

Rules:
- Max 1 alert per (severity, title) per minute (configurable via min_interval_sec)
- Max 10 alerts per severity per hour (configurable via max_per_hour)
- Max 50 alerts total per hour
- CRITICAL alerts bypass throttle (always sent)
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class ThrottleWindow:
    """Tracks send count and last-send timestamp for a single alert key."""

    count: int = 0
    last_sent: float = 0.0


class AlertThrottle:
    """Throttles alerts to prevent notification spam.

    Example:
        throttle = AlertThrottle(min_interval_sec=60, max_per_hour=10)
        if throttle.should_send("ERROR", "Daily limit breached"):
            # ... actually send the alert ...
    """

    def __init__(
        self,
        min_interval_sec: int = 60,
        max_per_hour: int = 10,
        total_max_per_hour: int = 50,
    ):
        self.min_interval = min_interval_sec
        self.max_per_hour = max_per_hour
        self.total_max_per_hour = total_max_per_hour
        self._windows: Dict[str, ThrottleWindow] = defaultdict(ThrottleWindow)
        self._hourly_counts: Dict[str, int] = defaultdict(int)
        self._total_hourly_count: int = 0
        self._hour_start = time.time()

    def should_send(self, severity: str, title: str) -> bool:
        """Check if alert should be sent (not throttled).

        Args:
            severity: INFO, WARNING, ERROR, or CRITICAL.
            title: Alert title used as part of the deduplication key.

        Returns:
            True if the caller should proceed to send the alert.
        """
        # CRITICAL always sends — bypass all throttle checks
        if severity == "CRITICAL":
            return True

        key = f"{severity}:{title}"
        now = time.time()

        # Reset hourly counters if the hour window has elapsed
        if now - self._hour_start > 3600:
            self._windows.clear()
            self._hourly_counts.clear()
            self._total_hourly_count = 0
            self._hour_start = now

        window = self._windows[key]

        # 1. Per-alert cooldown: max 1 per min_interval for identical (severity, title)
        if now - window.last_sent < self.min_interval:
            return False

        # 2. Hourly limit per severity
        if self._hourly_counts[severity] >= self.max_per_hour:
            return False

        # 3. Total hourly limit across all severities
        if self._total_hourly_count >= self.total_max_per_hour:
            return False

        # Update counters and allow send
        window.count += 1
        window.last_sent = now
        self._hourly_counts[severity] += 1
        self._total_hourly_count += 1
        return True

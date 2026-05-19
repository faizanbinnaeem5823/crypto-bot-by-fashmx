"""Alert system: Slack, Discord, Email routing with throttling."""

from .alert_manager import AlertManager
from .throttle import AlertThrottle

__all__ = ["AlertManager", "AlertThrottle"]
__version__ = "0.1.0"

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class IronRules:
    WITHDRAWALS_OFF: bool = True
    DEFAULT_RISK_PROFILE: str = "conservative"
    RECONCILIATION_INTERVAL_SEC: int = 60
    PAPER_MIN_DAYS: int = 90
    CAPITAL_FLOOR_USD: int = 500
    CAPITAL_HARD_CAP_USD: int = 5000

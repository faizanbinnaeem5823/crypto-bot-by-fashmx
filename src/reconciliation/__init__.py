"""
Crypto Trading Bot – Reconciliation Module (R3)
================================================
Periodically reconciles bot-recorded equity against the exchange.
"""

__version__ = "0.1.0"

from .reconciler import Reconciler, DriftRecord

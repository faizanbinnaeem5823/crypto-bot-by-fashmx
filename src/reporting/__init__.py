"""
Crypto Trading Bot - Reporting Module
======================================

Generates performance reports and visualisations:
    - Equity curve charts
    - Drawdown analysis
    - Trade statistics (win rate, Sharpe, profit factor)
    - Monthly / daily PnL breakdowns
    - HTML report generation
    - CSV / Parquet data export
"""

import logging

logger = logging.getLogger(__name__)

__version__ = "0.1.0"
__all__ = [
    "ReportGenerator",
    "PerformanceMetrics",
    "plot_equity_curve",
    "export_report_csv",
]

# Deferred imports
# from .generator import ReportGenerator
# from .metrics import PerformanceMetrics
# from .plots import plot_equity_curve
# from .export import export_report_csv

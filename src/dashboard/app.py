"""
CryptoBot Monitor Dashboard V1
READ-ONLY monitoring dashboard for dual-bot crypto trading system.
"""

import logging

try:
    import streamlit as st
    STREAMLIT_AVAILABLE = True
except ImportError:
    STREAMLIT_AVAILABLE = False
    st = None

logger = logging.getLogger(__name__)


def main():
    """Main entry point for dashboard."""
    if not STREAMLIT_AVAILABLE:
        logger.warning("Streamlit not installed - dashboard unavailable")
        return "Streamlit not installed"
    st.title("Crypto Trading Bot Dashboard")
    return "OK"

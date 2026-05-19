import pytest
import tempfile
import os
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path


@pytest.fixture
def temp_db():
    """Temporary DuckDB database for tests."""
    with tempfile.NamedTemporaryFile(suffix='.duckdb', delete=False) as f:
        path = f.name
    # Remove the empty file so DuckDB can create a fresh database
    if os.path.exists(path):
        os.unlink(path)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def mock_bot_config():
    """Mock bot configuration for tests."""
    return {
        "bot_id": "test_bot",
        "timeframe_band": "15m-4h",
        "initial_capital": Decimal("500"),
        "risk_profile": "conservative",
        "max_position_size": Decimal("0.05"),
        "daily_loss_limit": Decimal("7.50"),  # 1.5% of 500
    }


@pytest.fixture
def mock_risk_config():
    """Mock risk configuration."""
    return {
        "per_trade_risk_pct": 0.5,
        "daily_cap_pct": 1.5,
        "weekly_cap_pct": 4.0,
        "monthly_cap_pct": 8.0,
        "max_drawdown_kill_pct": 20.0,
        "reconciliation_interval_sec": 60,
    }


@pytest.fixture
def sample_candles():
    """Sample OHLCV candles for strategy tests."""
    import pandas as pd
    return pd.DataFrame({
        'open': [100.0, 101.0, 102.0, 101.5, 103.0, 104.0, 103.5, 105.0],
        'high': [101.5, 102.5, 103.0, 103.5, 105.0, 105.5, 106.0, 106.5],
        'low': [99.5, 100.5, 100.0, 101.0, 102.0, 103.0, 103.0, 104.0],
        'close': [101.0, 102.0, 101.5, 103.0, 104.0, 103.5, 105.0, 105.5],
        'volume': [1000, 1200, 900, 1100, 1300, 1000, 1500, 1200],
    })

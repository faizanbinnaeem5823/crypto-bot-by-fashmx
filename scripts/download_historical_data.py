import logging
import duckdb
from pathlib import Path

logger = logging.getLogger(__name__)

class HistoricalDataDownloader:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = duckdb.connect(db_path)
        self._init_tables()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS historical_candles (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                open DECIMAL,
                high DECIMAL,
                low DECIMAL,
                close DECIMAL,
                volume DECIMAL
            )
        """)

    def close(self):
        self.conn.close()

"""Integration tests for data pipeline."""
import pytest
import tempfile
import os
from pathlib import Path


class TestDataDownloaderIntegration:
    def test_downloader_import(self):
        import scripts.download_historical_data as dd
        assert hasattr(dd, 'HistoricalDataDownloader')

    def test_downloader_class_import(self):
        from scripts.download_historical_data import HistoricalDataDownloader
        assert HistoricalDataDownloader is not None

    def test_database_creation(self):
        from scripts.download_historical_data import HistoricalDataDownloader
        with tempfile.NamedTemporaryFile(suffix='.duckdb', delete=False) as f:
            db_path = f.name
        # Remove empty file so DuckDB can create a fresh database
        if os.path.exists(db_path):
            os.unlink(db_path)
        try:
            downloader = HistoricalDataDownloader(db_path=db_path)
            assert Path(db_path).exists()
            downloader.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

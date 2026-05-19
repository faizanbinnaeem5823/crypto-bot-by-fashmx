"""Healthcheck HTTP endpoint for the trading bot.

Provides:
- GET /health — Overall health (200=healthy, 503=unhealthy)
- GET /health/ready — Readiness probe (bot initialized and connected)
- GET /health/live — Liveness probe (bot hasn't deadlocked)
- GET /health/metrics — Current metrics snapshot (JSON)

Usage:
    from bot.healthcheck import HealthCheckServer

    health = HealthCheckServer(bot_runner, port=8081)
    await health.start()
    # ... later ...
    await health.stop()
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

logger = logging.getLogger(__name__)


class HealthState:
    """Shared health state between bot and healthcheck endpoint."""

    def __init__(self):
        self.initialized = False
        self.last_tick = datetime.now(timezone.utc)
        self.db_connected = False
        self.exchange_connected = False
        self.redis_connected = False
        self.kill_switch_state = "safe"
        self.daily_pnl = 0.0
        self.equity = 500.0
        self.error_count = 0

    def is_healthy(self) -> bool:
        """Check if bot is healthy."""
        age = (datetime.now(timezone.utc) - self.last_tick).total_seconds()
        if age > 300:  # No tick in 5 minutes
            return False
        if self.error_count > 10:
            return False
        if self.kill_switch_state == "triggered":
            return False
        return True

    def is_ready(self) -> bool:
        """Check if bot is ready to trade."""
        return self.initialized and self.db_connected and self.exchange_connected


class HealthCheckServer:
    """FastAPI healthcheck server."""

    def __init__(self, health_state: HealthState, port: int = 8081):
        self.state = health_state
        self.port = port
        self.app = FastAPI(title="CryptoBot Health")
        self._setup_routes()
        self._server = None

    def _setup_routes(self):
        @self.app.get("/health")
        async def health():
            if self.state.is_healthy():
                return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
            raise HTTPException(status_code=503, detail="Bot is unhealthy")

        @self.app.get("/health/ready")
        async def ready():
            if self.state.is_ready():
                return {"status": "ready"}
            raise HTTPException(status_code=503, detail="Bot not ready")

        @self.app.get("/health/live")
        async def live():
            age = (datetime.now(timezone.utc) - self.state.last_tick).total_seconds()
            if age < 300:
                return {"status": "live", "last_tick_age": age}
            raise HTTPException(status_code=503, detail="Bot appears deadlocked")

        @self.app.get("/health/metrics")
        async def metrics():
            return {
                "equity": self.state.equity,
                "daily_pnl": self.state.daily_pnl,
                "kill_switch": self.state.kill_switch_state,
                "db_connected": self.state.db_connected,
                "exchange_connected": self.state.exchange_connected,
                "initialized": self.state.initialized,
                "error_count": self.state.error_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    async def start(self):
        """Start healthcheck server in background."""
        import asyncio

        config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        asyncio.create_task(self._server.serve())
        logger.info(f"Healthcheck server started on port {self.port}")

    async def stop(self):
        """Stop healthcheck server."""
        if self._server:
            self._server.should_exit = True
            logger.info("Healthcheck server stopped")

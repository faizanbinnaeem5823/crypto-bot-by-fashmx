"""
Crypto Trading Bot — Core Bot Package.

This package contains the main entrypoint and orchestration logic that ties
together all modules: exchange clients, risk engine, execution, state
management, strategies, and reconciliation.

Modules
-------
- bot_runner : ``BotRunner`` — main orchestrator class
- config_loader : ``ConfigLoader`` — YAML configuration loading
- heartbeat : ``heartbeat_loop`` — background heartbeat task
- main : CLI entrypoint with argument parsing and signal handling
"""

from .bot_runner import BotRunner
from .config_loader import ConfigLoader

__all__ = ["BotRunner", "ConfigLoader"]

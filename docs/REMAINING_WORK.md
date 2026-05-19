# REMAINING WORK — Deep Analysis
**Date:** 2026-05-20
**Scope:** Gap analysis between what was built (Week 1 Days 1-7) and production readiness
**Honest assessment:** Infrastructure is ~85% complete. Business logic is ~30% complete. Integration is ~15% complete.

---

## EXECUTIVE SUMMARY

| Layer | Status | % Complete | What's Missing |
|-------|--------|-----------|----------------|
| Infrastructure (Docker, CI, VPS) | Green | 90% | Grafana dashboards, alert routing, log shipping |
| Risk Engine | Yellow | 70% | Wiring to actual trading loop, stress testing |
| Exchange Client | Yellow | 65% | Real Binance client exists but untested against live API |
| State Management | Yellow | 60% | DB retry works, but cross-bot state not integration-tested |
| Strategies | Red | 35% | Skeleton code exists, no walk-forward validation |
| Trading Bot Main Loop | Red | 10% | Does not exist. No bot entrypoint. |
| Data Pipeline | Yellow | 65% | Downloader works, but needs live candle streaming |
| Monitoring & Alerts | Red | 30% | Dashboard exists (mock data), no real alert routing |
| Paper Trading | Red | 20% | Paper broker exists but not connected to strategies |
| Testing | Yellow | 70% | 83 unit tests pass, no integration tests against real services |

**Bottom line:** You have excellent infrastructure and risk scaffolding. But there is no trading bot yet. The "bot" is a collection of modules — not a running system.

---

## DETAILED GAP ANALYSIS

### 1. THE BOT MAIN LOOP (CRITICAL — DOES NOT EXIST)

**Status:** Not started
**Priority:** CRITICAL
**Effort:** 3-5 days

There is no `src/bot/main.py` or `src/bot/bot_runner.py`. The trading bot — the thing that actually runs — does not exist. You cannot start the bot because there is no entrypoint.

What needs to be built:

```python
# src/bot/main.py — THIS FILE DOES NOT EXIST
async def main():
    # 1. Load config from YAML
    # 2. Initialize BinanceClient (testnet for paper)
    # 3. Initialize PaperBroker
    # 4. Initialize StateManager
    # 5. Initialize RiskEngine
    # 6. Initialize KillSwitch
    # 7. Initialize OrderManager
    # 8. Initialize Strategy
    # 9. Start Reconciler background task
    # 10. Start heartbeat background task
    # 11. MAIN LOOP:
    #       a. Fetch latest candles
    #       b. Run strategy.generate_signal()
    #       c. If signal != 0:
    #          - Calculate position size (risk engine)
    #          - Submit order (order manager)
    #          - Record trade (state manager)
    #       d. Update equity curve
    #       e. Run drawdown check
    #       f. Sleep until next candle
    # 12. Graceful shutdown on SIGTERM
```

**This is the single most important missing piece.** Without it, all the other modules are unused.

**Required new files:**
- `src/bot/__init__.py`
- `src/bot/main.py` — Async bot entrypoint with signal handling
- `src/bot/bot_runner.py` — Bot orchestrator that ties all modules together
- `src/bot/heartbeat.py` — Background heartbeat task
- `src/bot/config_loader.py` — YAML config loader with validation

---

### 2. REAL-TIME CANDLE STREAMING (CRITICAL)

**Status:** Skeleton only
**Priority:** CRITICAL
**Effort:** 2-3 days

The `scripts/download_historical_data.py` downloads historical data in batch. But the bot needs **live candles** as they form. This requires:

**WebSocket connection to Binance:**
- `src/exchange/binance_ws.py` — Async WebSocket client for live kline streams
- Auto-reconnect with exponential backoff
- Buffer management for partial candles
- Connection health monitoring

**Required new files:**
- `src/exchange/binance_ws.py` — WebSocket kline stream client
- `src/exchange/websocket_pool.py` — Connection pool for multiple symbol streams

**Without this, the bot cannot receive real-time price data and will not trade.**

---

### 3. STRATEGIES ARE SKELETONS (CRITICAL)

**Status:** Skeleton code — no backtesting validation
**Priority:** CRITICAL
**Effort:** 5-7 days

Current strategy code (`ema_crossover.py`, `rsi_macd_combo.py`) has the basic signal generation but:

**What's missing:**
- No walk-forward validation (WFO) — the gold standard for strategy validation
- No parameter optimization with proper train/test split
- No correlation analysis between strategies
- No regime-specific parameter sets
- No ATR-based stop-loss calculation
- No take-profit logic
- No trailing stop logic
- No position sizing integration with signal strength
- No strategy performance tracking over time

**What needs to be built:**
- `src/strategies/walk_forward.py` — Walk-forward analysis framework
- `src/strategies/parameter_grid.py` — Parameter optimization with train/test
- `src/strategies/signal_processor.py` — Convert raw signal to actionable trade signal
- `src/strategies/stop_loss.py` — ATR-based and fixed stop-loss calculator
- `src/strategies/take_profit.py` — Take-profit and trailing stop logic
- `src/strategies/performance_tracker.py` — Per-strategy P&L tracking
- `src/strategies/signal_strength.py` — Signal quality scoring (0.0 to 1.0)

**Per the master strategy, you need walk-forward Sharpe > 0.95 and PBO < 0.5 before paper trading.** None of this exists.

---

### 4. PAPER TRADING ORCHESTRATION (HIGH)

**Status:** PaperBroker exists but not connected
**Priority:** HIGH
**Effort:** 2-3 days

`PaperBroker` can simulate orders. But:

**What's missing:**
- No price feed for paper trades (needs market data)
- No realistic fill price simulation (uses passed price, not market)
- No paper-equity tracking over time
- No paper P&L calculation
- No virtual balance enforcement (can go negative)
- No paper trade logging to StateManager
- No switch from paper to live (mode transition)

**Required fixes:**
- PaperBroker needs market price source for realistic fills
- PaperBroker must enforce $500 virtual balance
- PaperBroker must record trades to DuckDB via StateManager
- PaperBroker must calculate realistic slippage

---

### 5. ALERT SYSTEM IS A CONFIG FILE (HIGH)

**Status:** YAML config only — no actual alert sending code
**Priority:** HIGH
**Effort:** 2-3 days

`config/alerts.yaml` exists with routing rules. But there is no code that reads this file and actually sends alerts.

**What's missing:**
- `src/alerts/alert_manager.py` — Reads config and routes alerts
- `src/alerts/slack_sender.py` — Sends Slack webhooks
- `src/alerts/discord_sender.py` — Sends Discord webhooks
- `src/alerts/email_sender.py` — Sends SMTP emails
- `src/alerts/alert_levels.py` — INFO/WARNING/ERROR/CRITICAL levels
- `src/alerts/throttle.py` — Prevents alert spam (e.g., max 1 per minute per type)
- Integration: KillSwitch.trigger() → alert
- Integration: Reconciler.drift() → alert
- Integration: Circuit breaker breach → alert

**Required new files:**
- `src/alerts/__init__.py`
- `src/alerts/alert_manager.py`
- `src/alerts/slack_sender.py`
- `src/alerts/discord_sender.py`
- `src/alerts/email_sender.py`
- `src/alerts/throttle.py`

---

### 6. METRICS & REPORTING PIPELINE (HIGH)

**Status:** Skeleton — calculates basic metrics but doesn't persist them
**Priority:** HIGH
**Effort:** 2-3 days

`MetricsReporter` calculates Sharpe, drawdown, etc. But:

**What's missing:**
- No Prometheus metrics export (the prometheus-client is in requirements but unused)
- No daily/weekly/monthly report generation
- No equity curve persistence (StateManager has it, but not integrated)
- No win/loss streak tracking
- No consecutive losing month detection (retirement trigger)
- No Sharpe degradation detection
- No per-strategy performance breakdown
- No CSV/JSON export for external analysis

**Required:**
- Wire MetricsReporter to StateManager equity data
- Export Prometheus metrics on `bot:equity`, `bot:drawdown`, `bot:daily_pnl`, `bot:win_rate`
- Add `src/reporting/daily_report.py` — Automated daily report generation
- Add retirement condition checks (Sharpe<0, 3 losing months, etc.)

---

### 7. NEWS PIPELINE IS A STUB (MEDIUM)

**Status:** Trivial keyword-based filter
**Priority:** MEDIUM (master strategy says V1 is $0 budget)
**Effort:** 3-5 days

`SentimentFilter` does simple keyword matching. The master strategy requires:

**What's missing:**
- CryptoPanic API integration (free tier)
- RSS feed monitoring (CoinDesk, Cointelegraph)
- Local transformer model (CryptoBERT or BGE-M3)
- Halt-only decision: extreme negative sentiment → pause trading
- NOT in the trade-decision path (Iron Rule #1)
- Text hallucination defense (per master strategy §10)
- Manipulation detection (per master strategy §10)

**Required new files:**
- `src/news/cryptopanic_client.py`
- `src/news/rss_fetcher.py`
- `src/news/transformer_classifier.py`
- `src/news/halt_decision.py`
- `src/news/cache.py`

**Note:** Master strategy defers this to after live profitability. Can wait.

---

### 8. TAX & COMPLIANCE (MEDIUM)

**Status:** Not started
**Priority:** MEDIUM
**Effort:** 2-3 days

Master strategy requires FBR compliance + Koinly integration.

**What's missing:**
- `src/tax/koinly_exporter.py` — Export trades to Koinly CSV format
- `src/tax/fbr_ledger.py` — FBR-compliant transaction log with bot_id tag
- `src/tax/pnl_calculator.py` — Per-trade P&L in PKR
- USD→PKR exchange rate fetching (SBP rate)
- Consolidated ledger with timestamps, fees, bot_id

**Required new files:**
- `src/tax/__init__.py`
- `src/tax/koinly_exporter.py`
- `src/tax/fbr_ledger.py`
- `src/tax/pnl_calculator.py`

---

### 9. WEBSOCKET INFRASTRUCTURE (HIGH)

**Status:** Not started
**Priority:** HIGH
**Effort:** 3-4 days

The bot needs real-time price data via WebSocket for:
- Live candle updates
- Order book depth (for slippage estimation)
- Account balance changes
- Order execution updates

**Required new files:**
- `src/exchange/binance_ws.py` — WebSocket connection manager
- `src/exchange/websocket_pool.py` — Multi-symbol connection pool
- `src/exchange/reconnection_handler.py` — Auto-reconnect with backoff

---

### 10. DASHBOARD USES MOCK DATA (MEDIUM)

**Status:** Streamlit skeleton with mock data
**Priority:** MEDIUM
**Effort:** 2-3 days

`src/dashboard/app.py` is well-structured but displays mock data. To make it real:

**What's missing:**
- Connect to DuckDB for real trades/equity data
- Connect to Redis for bot health status
- Real equity curve from StateManager
- Real open positions from exchange
- Real kill switch status
- Prometheus metrics display
- Auto-refresh with actual data

**Fix needed:** Replace all mock data generators with database queries.

---

### 11. LOGGING INFRASTRUCTURE (MEDIUM)

**Status:** Basic Python logging
**Priority:** MEDIUM
**Effort:** 1-2 days

**What's missing:**
- Structured JSON logging (for Loki ingestion)
- Log rotation
- Per-module log levels (configurable)
- Loki push integration (Docker compose has Loki, but nothing pushes to it)
- Sentry/Rollbar integration for crash reporting
- Audit log (separate from operational logs)

---

### 12. CONFIG PROMOTION/SYMBOL TIERS (LOW)

**Status:** Not started
**Priority:** LOW
**Effort:** 1-2 days

Master strategy references:
- `config/promotion_gates.yaml` — Symbol tier promotion criteria
- `config/symbol_tiers.yaml` — Tier 1/2/3 symbol lists with promotion rules

These config files don't exist. They're needed when the bot is profitable and ready to expand beyond BTC/ETH.

---

### 13. CCXT INTEGRATION (MEDIUM)

**Status:** Not used
**Priority:** MEDIUM
**Effort:** 2-3 days

`ccxt` is in `requirements.txt` but no code uses it. The master strategy mentions Freqtrade integration. Options:

- **Option A:** Use ccxt as the exchange abstraction layer (instead of custom BinanceClient)
- **Option B:** Remove ccxt from requirements (simpler, but less portable)
- **Option C:** Use Freqtrade as the strategy execution framework (major architectural change)

**Recommendation:** Keep ccxt. Wrap the exchange client with a ccxt-compatible interface for future exchange portability.

---

### 14. HEALTHCHECK & READINESS PROBES (MEDIUM)

**Status:** Docker healthchecks exist for infrastructure
**Priority:** MEDIUM
**Effort:** 1 day

**What's missing:**
- Bot-specific healthcheck endpoint (HTTP /health)
- Readiness probe: bot is initialized and connected
- Liveness probe: bot hasn't deadlocked
- Dependency health: DB, Redis, exchange all reachable

---

## PRIORITY MATRIX

### CRITICAL (Bot cannot run without these)

| # | Item | Effort | Dependencies |
|---|------|--------|-------------|
| 1 | Bot main loop (`src/bot/main.py`) | 3-5 days | All other modules |
| 2 | Real-time candle streaming (WebSocket) | 2-3 days | BinanceClient |
| 3 | Strategy walk-forward validation | 5-7 days | Data downloader |

### HIGH (Bot runs but is dangerous/incomplete)

| # | Item | Effort | Dependencies |
|---|------|--------|-------------|
| 4 | Paper trading orchestration | 2-3 days | Bot main loop |
| 5 | Alert system (Slack/Discord/Email) | 2-3 days | Config loader |
| 6 | Metrics pipeline (Prometheus export) | 2-3 days | StateManager |
| 7 | WebSocket infrastructure | 3-4 days | Async loop |

### MEDIUM (Important for production)

| # | Item | Effort | Dependencies |
|---|------|--------|-------------|
| 8 | Dashboard real data | 2-3 days | StateManager |
| 9 | News sentiment pipeline | 3-5 days | Transformer models |
| 10 | Tax & compliance (Koinly/FBR) | 2-3 days | Trade recording |
| 11 | Structured logging (Loki) | 1-2 days | Docker |
| 12 | CCXT integration layer | 2-3 days | Exchange client |
| 13 | Healthcheck endpoints | 1 day | Bot main loop |

### LOW (Nice to have)

| # | Item | Effort | Dependencies |
|---|------|--------|-------------|
| 14 | Symbol tier promotion configs | 1-2 days | Config system |
| 15 | Grafana dashboard JSON | 1 day | Prometheus metrics |
| 16 | Sentry crash reporting | 0.5 days | Logging |

---

## RECOMMENDED EXECUTION ORDER

### Phase 1: Bot Core (Week 2-3) — ~10-15 days
1. Build `src/bot/main.py` (the bot entrypoint)
2. Build `src/bot/bot_runner.py` (module orchestrator)
3. Build `src/bot/config_loader.py` (YAML config with validation)
4. Build `src/exchange/binance_ws.py` (WebSocket for live candles)
5. Build `src/strategies/stop_loss.py` + `take_profit.py`
6. Connect PaperBroker → StateManager for paper equity tracking
7. Run paper trading for 1 week with Bot A only

### Phase 2: Safety & Observability (Week 4-5) — ~8-12 days
8. Build `src/alerts/` (Slack/Discord/Email)
9. Wire alerts to KillSwitch, Reconciler, CircuitBreakers
10. Export Prometheus metrics
11. Connect dashboard to real data
12. Add structured JSON logging → Loki

### Phase 3: Validation (Week 6-7) — ~7-10 days
13. Build walk-forward analysis framework
14. Run WFO on both strategies
15. Check Sharpe > 0.95, PBO < 0.5
16. Optimize strategy parameters
17. Run extended paper trading (Bot A + Bot B)

### Phase 4: Polish (Week 8+) — ~5-8 days
18. Build tax export (Koinly CSV)
19. Build news sentiment pipeline
20. Add healthcheck endpoints
21. VPS migration (if still on local Docker)
22. Start 90-day paper trading gate

**Total estimated effort: 30-45 days to production-ready paper trading.**

---

## RISK ASSESSMENT: WHAT COULD GO WRONG

| Risk | Probability | Impact | Mitigation |
|------|:-----------:|:------:|------------|
| Bot main loop has race conditions | HIGH | CRITICAL | Extensive async testing, use asyncio.Lock for shared state |
| WebSocket disconnects silently | HIGH | HIGH | Heartbeat on WS, auto-reconnect with backoff |
| Strategy overfits in WFO | MEDIUM | HIGH | Strict train/test separation, PBO threshold |
| Paper trading ≠ live trading | HIGH | HIGH | Conservative slippage estimates, realistic fee modeling |
| Alert fatigue → operator ignores | MEDIUM | MEDIUM | Throttle alerts, tiered severity |
| Daily P&L reset fails on DST | LOW | MEDIUM | UTC-only timestamps, no DST in UTC |
| Redis failure breaks cross-bot kill | LOW | HIGH | Fallback to file-based kill state |

---

## HONEST VERDICT

**What you have:**
- Production-quality infrastructure (Docker, CI, testing)
- Excellent risk scaffolding (kill switch, circuit breakers, reconciler)
- Real Binance API client with HMAC signing
- Comprehensive backtest engine
- Well-documented, tested modules

**What you don't have:**
- A running bot (no main loop)
- Real-time data streaming
- Validated strategies (no WFO)
- Working alert system
- Live dashboard data
- Paper trading that actually simulates

**The gap:** ~30-45 days of focused development by a senior Python developer to go from "collection of modules" to "running trading bot with paper trading."

**The good news:** The foundation is solid. Every module is well-designed and tested. Building the remaining pieces is "connecting the dots" rather than "rebuilding the foundation."

---

*"The bot is not the edge. The discipline to build it right is the edge."*

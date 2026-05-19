# Red Team Review

**Date:** 2026-05-20
**Reviewer:** AI Risk Analyst (Senior Hedge Fund Risk Engineer)
**Scope:** Week 1 Days 1-7 Deliverables -- Full codebase audit
**Methodology:** Static analysis, architecture review, failure-mode injection, adversarial threat modeling ("3 AM Sunday, BTC -20% in 10 min" scenario)

---

## Executive Summary

This codebase presents a **structurally sound architecture with critical safety gaps** that render it **non-tradable in its current state**. The foundational design decisions are sound: Decimal for monetary values, proper .gitignore for secrets, sensible Iron Rules, conservative risk limits, and a well-thought-out VPS hardening script. However, there is a **systematic pattern of incomplete wiring** -- safety mechanisms exist as stubs or in-memory objects that are never connected to the actual trading path. The kill switch, circuit breakers, reconciliation engine, and alerting system are all "dead code" that log messages but do not stop trading flows. This is the single most dangerous pattern: it creates the *illusion* of safety while providing zero protection.

At 3 AM on a Sunday when Bitcoin drops 20% in 10 minutes, this system would: (1) fail to detect the drawdown breach because max_drawdown is never checked in the hot path, (2) fail to trigger the kill switch because it's per-instance and not wired to order submission, (3) fail to send alerts because there's no alert sender implementation, (4) continue placing orders through mock exchange calls that return fake success responses, and (5) record those fake trades in DuckDB with no reconciliation against actual exchange state. The operator would wake up to a database full of phantom trades and no idea what actually happened.

The good news: every finding is fixable. The architecture is correct; the implementation needs to be completed. The hardening script (harden_vps.sh) is production-quality and shows the team can write secure infrastructure code. The gap is entirely in the Python trading logic layer.

---

## CRITICAL Findings

### R1: Kill Switch Is a Decorator -- It Does NOT Stop Trading
**Severity:** CRITICAL
**File:** `src/risk/kill_switch.py`, `src/risk/risk_engine.py`, `src/execution/order_manager.py`, `src/exchange/binance_client.py`

- **Description:** The `KillSwitch` class is a pure in-memory state machine (`SAFE -> ARMED -> TRIGGERED`). `RiskEngine.check_trade_allowed()` queries it and returns `(False, "Kill switch triggered")` when triggered. **However, no trading code path actually calls `check_trade_allowed()`.** `OrderManager.submit_order()` does not call it. `BinanceClient.place_market_order()` does not call it. The main trading loop (which doesn't exist yet) does not call it. The kill switch is an island. Worse: it is **per-bot-instance**, so Bot A triggering its kill switch does not affect Bot B. On VPS restart, all kill switches reset to `SAFE` because state is pure memory with no persistence.
- **Impact:** In a flash-crash scenario, the kill switch provides zero protection. The system will continue trading regardless of kill state. The operator believes the system is protected (Iron Rule #9: "Kill-switch tested monthly") but the wiring doesn't exist.
- **Fix:** (1) Add kill-switch check as a MANDATORY gate in `OrderManager.submit_order()` -- raise `KillSwitchError` if triggered. (2) Make the kill switch **shared across processes** using Redis pub/sub or a PostgreSQL advisory lock with a `kill_switch_state` table that both bots poll every tick. (3) Persist kill state to the database and restore on startup. (4) Wire the check into EVERY entry point that can move capital:

```python
# In OrderManager.submit_order():
async def submit_order(self, symbol, side, quantity, price=None):
    # CRITICAL GATE: Kill switch check
    if self.kill_switch.is_triggered():
        raise KillSwitchError("Trading halted - kill switch is TRIGGERED")
    if not self.risk_engine.check_trade_allowed(portfolio_value, daily_pnl)[0]:
        raise RiskViolation("Trading not allowed by risk engine")
    # ... rest of submission

# In state_manager.py - add persistence:
def persist_kill_state(self, state: str, reason: str):
    self.conn.execute("""
        INSERT OR REPLACE INTO kill_switch (singleton_key, state, reason, updated_at)
        VALUES (1, ?, ?, CURRENT_TIMESTAMP)
    """)

def load_kill_state(self) -> str:
    row = self.conn.execute("SELECT state FROM kill_switch WHERE singleton_key=1").fetchone()
    return row[0] if row else "SAFE"
```

---

### R2: BinanceClient Is a Complete Stub -- No Real API Integration
**Severity:** CRITICAL
**File:** `src/exchange/binance_client.py`

- **Description:** `get_balance()` returns hardcoded `Decimal("1000")` with a `# Mock` comment. `place_market_order()` returns a hardcoded dict `{"status": "filled", ...}` without making ANY HTTP request. There is no HMAC-SHA256 signature generation, no request signing, no timestamp header, no recvWindow handling, no actual Binance API call. The `httpx.AsyncClient` is created but never used for real requests.
- **Impact:** The entire "trading" system is a simulation that never touches the exchange. When the operator flips `BINANCE_TESTNET=false`, the code will still return mock responses while the operator believes live trades are executing. This is a catastrophic safety gap -- the system cannot actually trade, hedge, or stop out.
- **Fix:** Implement full Binance API integration with:

```python
import hmac, hashlib, time

async def _signed_request(self, method: str, path: str, params: dict = None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query_string = urlencode(params)
    signature = hmac.new(
        self.api_secret.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": self.api_key}
    response = await self.client.request(method, path, params=params, headers=headers)
    if response.status_code == 429:
        raise RateLimitError("Binance rate limit exceeded")
    if response.status_code == 418:
        raise IPBanError("Binance IP banned")
    response.raise_for_status()
    return response.json()

async def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict:
    return await self._signed_request("POST", "/api/v3/order", {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "type": "MARKET",
        "quantity": str(quantity),
    })
```

---

### R3: Reconciliation Is Configured But Does Not Exist
**Severity:** CRITICAL
**File:** `config/bot_a.yaml`, `config/bot_b.yaml`, `src/` (missing module)

- **Description:** Both bot configs specify `reconciliation: { interval_sec: 60, max_drift_usd: 1.00 }`. Iron Rule #6 states "Reconciliation runs every 60 seconds per bot." **There is no reconciliation module, no reconciliation loop, no code that reads this config.** The `StateManager` has no method to compare bot-recorded positions/balances against exchange-reported positions/balances. Drift detection is entirely absent.
- **Impact:** If a trade executes on the exchange but fails to be recorded in the bot's database (network failure, DB error, process crash), the bot will operate with stale state. Position sizing will be wrong. Daily P&L calculations will be wrong. Risk limits will be based on phantom data. In the extreme case, the bot could double-expose thinking it's flat.
- **Fix:** Create `src/reconciliation/reconciler.py` that:

```python
class Reconciler:
    async def reconcile(self):
        # 1. Query exchange for actual balances
        exchange_balance = await self.exchange.get_balance("USDT")
        # 2. Query bot state for recorded balance
        bot_balance = self.state_manager.get_latest_equity()
        # 3. Calculate drift
        drift = abs(exchange_balance - bot_balance.cash)
        if drift > self.max_drift_usd:
            logger.critical(f"DRIFT DETECTED: exchange={exchange_balance} bot={bot_balance.cash} drift=${drift}")
            self.risk_engine.kill_switch.trigger()
            await self.alert_manager.send_alert("reconciliation_drift", severity="critical", details={...})
```
Run this every `interval_sec` seconds in the main trading loop. **Trigger the kill switch on drift exceeding threshold.**

---

## HIGH Findings

### R4: Max Drawdown Circuit Breaker Is Orphan Code
**Severity:** HIGH
**File:** `src/risk/circuit_breakers.py`

- **Description:** `CircuitBreakers.check_max_drawdown(peak, current)` returns `True` when drawdown exceeds the configured kill percentage, but **this method is never called from any trading code path**. The daily limit check IS called from `RiskEngine.check_trade_allowed()`, but the drawdown kill is completely disconnected. There is no code that tracks peak equity, no periodic drawdown check, no wiring to the kill switch when the condition triggers.
- **Impact:** During a sustained market decline, the system has no automatic circuit breaker. The bot will continue taking losses past the configured 20% max drawdown kill point until capital is exhausted or manual intervention occurs.
- **Fix:** Add a periodic drawdown check in the main trading loop after every equity snapshot:

```python
# In the main loop, after updating equity:
peak_equity = self.state_manager.get_peak_equity()  # Track all-time high
current_equity = self.state_manager.get_latest_equity()
if self.circuit_breakers.check_max_drawdown(peak_equity, current_equity):
    self.kill_switch.trigger()
    logger.critical("Max drawdown circuit breaker triggered - halting all trading")
```
Store peak equity persistently (not in memory) and load it on startup.

---

### R5: OrderManager.submit_order() Is a No-Op
**Severity:** HIGH
**File:** `src/execution/order_manager.py`

- **Description:** `submit_order()` validates order size, then returns `{"status": "submitted"}` without calling any exchange client, broker, or external system. It does not interact with `BinanceClient` or `PaperBroker`. The `paper` flag is stored but never used to dispatch to the appropriate executor.
- **Impact:** No orders actually get placed. The trading loop will "submit" orders, record them in state, and proceed as if they executed. This creates phantom trade records that diverge from reality.
- **Fix:** Wire the order manager to an exchange client:

```python
class OrderManager:
    def __init__(self, bot_id: str, exchange_client, paper_broker, paper: bool = True):
        self.bot_id = bot_id
        self.exchange_client = exchange_client  # BinanceClient
        self.paper_broker = paper_broker         # PaperBroker
        self.paper = paper

    async def submit_order(self, symbol, side, quantity, price=None):
        if not self.validate_order_size(quantity, symbol):
            raise ValueError(f"Order size {quantity} below minimum")
        if self.paper:
            return self.paper_broker.place_market_order(symbol, side, quantity, price)
        else:
            return await self.exchange_client.place_market_order(symbol, side, quantity)
```

---

### R6: No Database Connection Resilience
**Severity:** HIGH
**File:** `src/state/state_manager.py`

- **Description:** `StateManager` opens a DuckDB connection in `__init__` and uses it directly with no error handling. All methods (`record_trade`, `update_equity`, `heartbeat`) execute SQL without try/except blocks, no retry logic, no connection health checks, no handling of disk-full errors, corrupted DB, or concurrent access conflicts. DuckDB in single-writer mode will throw `duckdb.ConnectionException` if two processes write simultaneously.
- **Impact:** If the DB connection drops mid-trade (disk full, file lock from another process, DB corruption), the `record_trade()` call raises an unhandled exception. The trade may have executed on the exchange but never been recorded. On restart, the bot has no memory of the trade. Position sizing will be wrong. P&L tracking will be permanently corrupted.
- **Fix:** (1) Add connection retry with exponential backoff. (2) Use SQLite/WAL mode or PostgreSQL for concurrent access. (3) Wrap all DB operations:

```python
def record_trade(self, trade: Trade):
    for attempt in range(3):
        try:
            self.conn.execute("...", params)
            return
        except duckdb.ConnectionException as e:
            if attempt < 2:
                time.sleep(0.1 * (2 ** attempt))
                self._reconnect()
            else:
                raise StateManagerError(f"Failed to record trade after 3 attempts: {e}")
```
(4) Each bot MUST use its own DB file to avoid lock contention.

---

### R7: Timestamps Are Not Consistently UTC
**Severity:** HIGH
**File:** `src/state/state_manager.py`, `src/strategies/base_strategy.py`

- **Description:** `StateManager._init_tables()` uses `TIMESTAMP` (no timezone) and `CURRENT_TIMESTAMP` (server-local time). Python code creates `Trade` objects with `datetime.now(timezone.utc)` in tests, but the `Trade` dataclass doesn't enforce UTC. `BaseStrategy.on_entry/on_exit` takes `pd.Timestamp` which may or may not be timezone-aware. When comparing heartbeat ages, trade timestamps, and equity snapshots across bot restarts, timezone mismatches will produce incorrect staleness calculations.
- **Impact:** Heartbeat monitoring will report false positives/negatives. Trade history ordering will be wrong around DST transitions or if the VPS timezone changes. Reconciliation drift detection based on timestamp comparison will be unreliable.
- **Fix:** (1) Enforce UTC everywhere. (2) Change DuckDB schema: `timestamp TIMESTAMPTZ`. (3) Add validation to `Trade.__post_init__`:

```python
@dataclass
class Trade:
    # ... fields ...
    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            raise ValueError("Trade timestamp must be timezone-aware (UTC)")
        if self.timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("Trade timestamp must be UTC")
```
(4) Use `datetime.now(timezone.utc)` exclusively -- never `datetime.now()` without tz.

---

### R8: Daily P&L Has No Day-Boundary Tracking
**Severity:** HIGH
**File:** `src/risk/circuit_breakers.py`

- **Description:** `check_daily_limit()` takes `daily_pnl` as a parameter, but **no code calculates daily P&L from trade history with proper day boundaries**. There is no midnight reset, no sliding 24-hour window, no tracking of which trades fall within "today." The daily cap is configured at 1.5% but there's no mechanism to enforce it against a rolling day.
- **Impact:** The daily loss limit is unenforceable. The circuit breaker check will pass regardless of actual daily losses because the `daily_pnl` value is never computed correctly. Bot could lose 5%, 10%, or more in a single day.
- **Fix:** Implement proper daily P&L tracking:

```python
def get_daily_pnl(self) -> Decimal:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = self.conn.execute("""
        SELECT COALESCE(SUM(pnl), 0) FROM trades
        WHERE bot_id = ? AND timestamp >= ? AND status = 'closed'
    """, (self.bot_id, today_start)).fetchone()
    return Decimal(str(result[0]))
```
Call this in the main loop and pass the result to `check_daily_limit()`.

---

### R9: PaperBroker Has Zero Slippage and Zero Fees
**Severity:** HIGH
**File:** `src/exchange/paper_broker.py`

- **Description:** `PaperBroker.place_market_order()` executes at the exact requested price with no slippage simulation and no fee deduction. Binance spot charges 0.1% per trade (0.05% with BNB). In volatile crypto markets, market order slippage can be 0.1-0.5% or more during high-volatility periods.
- **Impact:** Paper trading results will be unrealistically optimistic. A strategy that appears profitable at 0% slippage/fees may be a loser in reality. The promotion gates (sharpe > 1.2, win rate > 50%) will be met with phantom performance. When promoted to live, the operator will see immediate underperformance.
- **Fix:** Add realistic slippage and fee simulation:

```python
FEE_RATE = Decimal("0.001")  # 0.1% Binance spot fee
SLIPPAGE_PCT = Decimal("0.0005")  # 0.05% average slippage

def place_market_order(self, symbol, side, quantity, price):
    # Apply slippage against the trader
    slippage = price * SLIPPAGE_PCT
    executed_price = price + slippage if side == "BUY" else price - slippage
    fee = quantity * executed_price * FEE_RATE
    cost = quantity * executed_price
    if side == "BUY":
        self.balances[quote] -= (cost + fee)
        self.positions[base] += quantity
    else:
        self.positions[base] -= quantity
        self.balances[quote] += (cost - fee)
    # Record fee in trade for accurate P&L
```

---

### R10: No Rate Limiting, Retry, or Error Handling on Exchange Calls
**Severity:** HIGH
**File:** `src/exchange/binance_client.py`

- **Description:** `exchange.yaml` configures `rate_limit_requests_per_second: 10` and `max_retries: 3`, but `BinanceClient` completely ignores these settings. No rate limiter is implemented. No retry logic for failed requests. No handling of HTTP 429 (rate limited), 418 (IP banned), 5xx (exchange down), or network timeouts. The 30-second timeout is hardcoded with no request-specific overrides.
- **Impact:** During high-volatility periods when the bot needs to act fast (cancel orders, place hedge orders, check balances), API calls may be rate-limited by Binance. Without handling, the bot will crash with unhandled exceptions or hang indefinitely. Two bots hitting the same API endpoint will double the request rate, increasing ban probability.
- **Fix:** Implement a token-bucket rate limiter and resilient retry:

```python
import asyncio
from asyncio import Semaphore

class BinanceClient:
    def __init__(self, api_key, api_secret, testnet=True, rate_limit=10, max_retries=3):
        # ... existing init ...
        self._rate_limiter = Semaphore(rate_limit)  # Token bucket
        self.max_retries = max_retries
        self._request_times = deque(maxlen=rate_limit)

    async def _rate_limited_request(self, method, path, **kwargs):
        async with self._rate_limiter:
            for attempt in range(self.max_retries):
                try:
                    response = await self.client.request(method, path, **kwargs)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        await asyncio.sleep(retry_after)
                        continue
                    if response.status_code >= 500:
                        wait = 2 ** attempt
                        await asyncio.sleep(wait)
                        continue
                    response.raise_for_status()
                    return response
                except httpx.TimeoutException:
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)
```

---

### R11: Both Bots Trade Same Symbols Without Coordination
**Severity:** HIGH
**Files:** `config/bot_a.yaml`, `config/bot_b.yaml`

- **Description:** Both Bot A (conservative, 15m-4h) and Bot B (experimental, 5m-15m) are configured to trade BTC/USDT and ETH/USDT. There is no bot-to-bot coordination mechanism, no signal deduplication, no "one position per symbol across all bots" enforcement, no cross-bot exposure aggregation. They operate in complete isolation.
- **Impact:** Bots can take opposing positions on the same symbol (A long, B short), effectively hedging each other and paying fees for zero net exposure. Alternatively, they can stack correlated long positions, doubling risk beyond the intended per-trade limits. The combined risk across bots is not checked against the global daily/weekly/monthly caps. On a flash-crash, both bots could attempt to exit simultaneously, causing race conditions on shared exchange rate limits.
- **Fix:** (1) Assign distinct symbols per bot (e.g., Bot A = BTC/USDT only, Bot B = ETH/USDT only) until a cross-bot risk aggregator is built. (2) Create a shared `CrossBotRiskManager` using Redis to track combined exposure:

```python
class CrossBotRiskManager:
    def __init__(self, redis_client):
        self.redis = redis_client

    def get_combined_position(self, symbol: str) -> Decimal:
        # Sum positions across all bots from Redis
        keys = self.redis.keys(f"position:{symbol}:*")
        return sum(Decimal(self.redis.get(k)) for k in keys)

    def can_open_position(self, symbol: str, size: Decimal, max_total: Decimal) -> bool:
        current = self.get_combined_position(symbol)
        return current + size <= max_total
```

---

### R12: No Alert Sender Implementation
**Severity:** HIGH
**Files:** `config/alerts.yaml`, `src/` (missing module)

- **Description:** `alerts.yaml` configures Slack, Discord, and Email alerting with webhook URLs and severity levels. **There is no alert sender module.** No code reads this configuration. No alerts are sent when the kill switch triggers, when daily limits breach, or when heartbeats go stale. The alerting configuration is dead config.
- **Impact:** When the system fails at 3 AM, nobody knows. The operator discovers the problem hours later when they manually check Grafana (which may also be down). The kill switch can trigger, circuit breakers can fire, reconciliation can detect drift -- all silently.
- **Fix:** Create `src/alerts/alert_manager.py`:

```python
class AlertManager:
    def __init__(self, config: dict):
        self.channels = self._init_channels(config)

    async def send_alert(self, rule_name: str, severity: str, details: dict):
        channels = self.config.get(rule_name, {}).get("channels", [])
        for channel in channels:
            if channel == "slack":
                await self._send_slack(rule_name, severity, details)
            elif channel == "discord":
                await self._send_discord(rule_name, severity, details)
            elif channel == "email":
                await self._send_email(rule_name, severity, details)

    async def _send_slack(self, rule, severity, details):
        async with httpx.AsyncClient() as client:
            await client.post(self.slack_webhook, json={
                "text": f"[CRITICAL] {rule}: {details}"
            })
```
Wire it into: kill switch trigger, daily limit breach, max drawdown kill, reconciliation drift, heartbeat stale, uncaught exceptions.

---

## MEDIUM Findings

### R13: Position Sizing Lacks Hard Cap Enforcement
**Severity:** MEDIUM
**File:** `src/risk/risk_engine.py`

- **Description:** `calculate_position_size()` computes `portfolio_value * risk_per_trade * signal_strength`. The `signal_strength` parameter has no upper bound validation in the trading loop (it's passed from strategy code which could return values > 1.0). There is no check against `IronRules.CAPITAL_HARD_CAP_USD` ($5000). No per-symbol maximum position limit exists. No check that the computed position doesn't exceed available balance.
- **Impact:** A strategy bug or unexpected `signal_strength` value could compute a position size exceeding the portfolio. The trade would fail on the exchange (if real) or create impossible state (if paper).
- **Fix:** Add guardrails:

```python
def calculate_position_size(self, portfolio_value: Decimal, signal_strength: float, 
                            available_balance: Decimal) -> Decimal:
    signal_strength = max(0.0, min(1.0, signal_strength))  # Clamp
    risk_per_trade = Decimal(str(self.config["per_trade_risk_pct"])) / Decimal("100")
    size = portfolio_value * risk_per_trade * Decimal(str(signal_strength))
    # Hard caps
    size = min(size, available_balance * Decimal("0.95"))  # Leave 5% buffer
    size = min(size, Decimal(str(self.config.get("max_position_size", "999999"))))
    return max(Decimal("0"), size.quantize(Decimal("0.00001")))
```

---

### R14: SentimentFilter Is Trivial Keyword Matcher
**Severity:** MEDIUM
**File:** `src/news/sentiment_filter.py`

- **Description:** The sentiment filter uses a 13-word keyword list (`rally`, `bull`, `crash`, `hack`, etc.) and simple word counting. A headline like "Analysts say crash in BTC unlikely after hack attempt fails" would trigger a negative halt despite being bullish. A tweet containing "Just crashed my bike, but BTC is rallying" would trigger negative. There is no NLP, no context awareness, no negation handling.
- **Impact:** False trading halts from benign news containing keywords. Missed legitimate risk signals that don't use exact keywords. The halt mechanism is unreliable.
- **Fix:** Replace with a proper NLP model (Hugging Face `finbert-tone` or similar) with confidence thresholds. Keep the keyword filter as a fast pre-filter only:

```python
class SentimentFilter:
    def __init__(self, halt_threshold=-0.8):
        from transformers import pipeline
        self.classifier = pipeline("sentiment-analysis", model="yiyanghkust/finbert-tone")
        self.halt_threshold = halt_threshold

    def should_halt_trading(self, text: str) -> bool:
        # Fast keyword pre-filter
        if not any(w in text.lower() for w in self.keywords):
            return False
        # Deep sentiment analysis
        result = self.classifier(text[:512])[0]  # Model token limit
        return result["label"] == "negative" and result["score"] > abs(self.halt_threshold)
```

---

### R15: Dashboard Is a Stub with No Kill Switch Button
**Severity:** MEDIUM
**File:** `src/dashboard/app.py`

- **Description:** The dashboard is essentially empty -- it just renders a title. There is no bot health display, no P&L chart, no open positions table, no kill switch button, no mobile-responsive layout. The `docker-compose.yml` binds it to `127.0.0.1:8501` which is not externally accessible without SSH tunneling.
- **Impact:** The operator cannot monitor bot health or trigger the kill switch from mobile. In an emergency, they need SSH access to the VPS to stop trading -- unacceptable for a production trading system.
- **Fix:** Build out the dashboard with: (1) Real-time P&L and equity curve from DuckDB, (2) Open positions table, (3) **Big red KILL SWITCH button** that calls `kill_switch.trigger()` via API, (4) Bot health indicators (heartbeat age, last trade time), (5) Mobile-responsive layout, (6) Consider exposing through a reverse proxy with authentication.

---

### R16: No Input Validation on Candle Data
**Severity:** MEDIUM
**File:** `src/strategies/base_strategy.py`

- **Description:** `validate_dataframe()` checks for required columns, DatetimeIndex, and minimum row count. It does NOT validate: NaN/Inf values in OHLCV, negative prices, zero volume, timestamp ordering, duplicate timestamps, price inversions (high < low, close > high, close < low). Strategies will produce garbage signals from garbage data.
- **Impact:** Exchange API returning partial/corrupted data will produce false signals. A NaN in the close column will propagate through EMA calculations producing NaN signals that evaluate to False (no trade) -- silent data loss. An inverted high/low will cause incorrect indicator values.
- **Fix:** Add comprehensive validation:

```python
@staticmethod
def validate_dataframe(df: pd.DataFrame) -> None:
    # ... existing checks ...
    # Check for NaN/Inf
    if df.isin([np.nan, np.inf, -np.inf]).any().any():
        bad_cols = df.columns[df.isin([np.nan, np.inf, -np.inf]).any()].tolist()
        raise ValueError(f"DataFrame contains NaN/Inf in columns: {bad_cols}")
    # Check price invariants
    if not (df["low"] <= df["high"]).all():
        raise ValueError("low > high found in data")
    if not (df["close"].between(df["low"], df["high"])).all():
        raise ValueError("close outside low-high range")
    if not (df["open"].between(df["low"], df["high"])).all():
        raise ValueError("open outside low-high range")
    # Check timestamps are monotonic
    if not df.index.is_monotonic_increasing:
        raise ValueError("Timestamps are not monotonically increasing")
    if df.index.duplicated().any():
        raise ValueError(f"Duplicate timestamps found: {df.index[df.index.duplicated()].tolist()}")
```

---

### R17: Strategy Signal Uses float for Price
**Severity:** MEDIUM
**File:** `src/strategies/base_strategy.py`

- **Description:** `Signal.price: float` uses Python float for price. While backtest performance is acceptable with float, live trading should use `Decimal` throughout to avoid floating-point precision issues with exchange APIs that expect exact precision. `on_entry/on_exit` also use `float` for prices and P&L calculations.
- **Impact:** Float precision errors accumulate over thousands of trades. `0.1 + 0.2 != 0.3` in float arithmetic. Position sizing discrepancies between bot calculation and exchange execution.
- **Fix:** Change `Signal.price` to `Decimal` and update all strategy code to use `Decimal` for price values.

---

### R18: Docker Compose Bots Run as Root
**Severity:** MEDIUM
**File:** `docker-compose.yml`

- **Description:** Both `bot-a` and `bot-b` services use the `freqtradeorg/freqtrade:stable` image without specifying a `user:` directive. The Freqtrade image runs as root inside the container by default. While secrets are mounted read-only, a compromised bot process running as root has full container access and could escape via kernel vulnerabilities.
- **Impact:** Container escape vulnerability. Root inside container = potential host compromise if a container breakout CVE exists.
- **Fix:** Add `user: "1000:1000"` or a cryptobot-specific user ID to both bot services. Verify Freqtrade supports non-root execution (it does with proper volume permissions).

---

### R19: Missing `.dockerignore`
**Severity:** MEDIUM
**File:** (missing)

- **Description:** No `.dockerignore` file exists. The dashboard Dockerfile uses `COPY src/dashboard/ /app/src/dashboard/` which is safe, but future developers may add `COPY . /app` for convenience, which would include `.env`, `secrets/`, `*.pem`, and other sensitive files in the Docker image layer cache.
- **Impact:** Secrets baked into Docker images. Image layers are immutable -- even deleting the file later doesn't remove it from layer history.
- **Fix:** Create `.dockerignore`:

```
.env
.env.*
secrets/
*.pem
*.key
*.p12
*.pfx
.git/
__pycache__/
*.pyc
data/
*.duckdb
*.db
```

---

## LOW Findings

### R20: Prometheus/Grafana/Loki Use `latest` Tag
**Severity:** LOW
**File:** `docker-compose.yml`

- **Description:** `prom/prometheus:latest`, `grafana/grafana:latest`, and `grafana/loki:latest` use floating tags. Non-reproducible builds. Supply chain risk if a compromised image is pushed to the registry.
- **Impact:** Different deployments may run different versions. A malicious image update could compromise the monitoring stack.
- **Fix:** Pin to specific digests: `prom/prometheus:v2.51.0@sha256:...`

---

### R21: Streamlit CORS Disabled
**Severity:** LOW
**File:** `docker-compose.yml` (dashboard service)

- **Description:** `--server.enableCORS=false` disables cross-origin request protection on the Streamlit dashboard. Combined with binding to `0.0.0.0`, this increases XSS/CSRF risk if the dashboard is exposed beyond localhost.
- **Impact:** Malicious websites could embed the dashboard or make cross-origin requests to it.
- **Fix:** Enable CORS: `--server.enableCORS=true`, and access the dashboard through a reverse proxy with proper authentication.

---

### R22: No Healthcheck on Custom Trading Logic
**Severity:** LOW
**File:** `docker-compose.yml`

- **Description:** Bot healthchecks call Freqtrade's built-in `/api/v1/ping`, which only verifies the Freqtrade process is running. It does NOT verify: (1) the custom Python trading code is executing, (2) the strategy is generating signals, (3) the database is writable, (4) the exchange connection is alive, (5) heartbeats are being updated.
- **Impact:** Docker Swarm/Kubernetes will consider the bot "healthy" even if the trading loop is dead. Auto-restart won't trigger. The operator gets no warning.
- **Fix:** Add a custom healthcheck endpoint that verifies all critical subsystems:

```python
async def healthcheck():
    checks = {
        "db": state_manager.can_write(),
        "exchange": await exchange_client.ping(),
        "heartbeat": state_manager.get_heartbeat_age() < 120,
        "strategy": strategy_last_signal_time > time.time() - 300,
    }
    if not all(checks.values()):
        return JSONResponse({"status": "unhealthy", "checks": checks}, status_code=503)
    return {"status": "healthy", "checks": checks}
```

---

### R23: Tests Only Test Imports, Not Behavior
**Severity:** LOW
**File:** `tests/unit/*.py`

- **Description:** Most test classes have import tests (`test_XYZ_import`) that verify modules can be imported but don't test actual behavior under failure conditions. There are no tests for: DB connection failure, kill switch actually stopping order submission, circuit breaker triggering, drawdown calculation with edge cases (peak=0), rate limit handling, order submission when exchange is down.
- **Impact:** The test suite gives false confidence. Refactors may break critical safety code without test failures.
- **Fix:** Add adversarial tests: test that `submit_order()` raises when kill switch is triggered, test that `record_trade()` retries on DB error, test circuit breaker with exactly-at-limit values, test drawdown=0 edge case.

---

### R24: `duckdb_data` Volume Declared But Unused
**Severity:** LOW
**File:** `docker-compose.yml`

- **Description:** The `duckdb_data` named volume is declared in the volumes section but never mounted by any service. Each bot's DuckDB file would need to be in a persistent volume to survive container restarts, but the volume isn't connected.
- **Impact:** On container restart, all DuckDB state (trades, equity curve, heartbeats) is lost unless the DB file is explicitly mounted.
- **Fix:** Mount `duckdb_data` to the appropriate service, or switch both bots to use PostgreSQL (which IS properly persisted via `postgres_data`).

---

## Recommended Priority Order

1. **Fix R1 (Kill Switch Wiring) FIRST** -- This is the #1 priority because it's the most dangerous illusion. The team believes they have a safety mechanism that does nothing. In a crisis, the operator will rely on it and it will fail. Every hour of false confidence increases risk. Wire the kill switch into `OrderManager.submit_order()`, persist state to DB, make it cross-bot via Redis.

2. **Fix R2 (Real API Integration) SECOND** -- The system cannot trade. The stub must be replaced with real Binance API calls before any capital is deployed. This is prerequisite for all other testing.

3. **Fix R3 (Reconciliation) THIRD** -- Without reconciliation, the bot operates on potentially corrupted state. This must be in place before going live. The kill switch should trigger on drift > $1.

4. **Fix R4 (Drawdown Circuit Breaker) + R8 (Daily P&L) FOURTH** -- These are the core risk controls. Wire the drawdown check into the main loop. Implement proper daily P&L with midnight boundaries.

5. **Fix R12 (Alerts) FIFTH** -- If the system fails silently at 3 AM, nobody can respond. Alerts must fire for every kill switch, circuit breaker, and reconciliation drift event.

6. **Fix R9 (Slippage/Fees) SIXTH** -- Before paper trading results can be trusted, the simulation must be realistic. Add slippage and fees to PaperBroker.

7. **Fix R6 (DB Resilience) + R7 (UTC Timestamps) SEVENTH** -- These are data integrity foundations. Wrong timestamps and DB failures will corrupt all downstream metrics.

8. **Fix R10 (Rate Limiting) + R11 (Bot Coordination) EIGHTH** -- Exchange resilience and cross-bot risk management for dual-bot operation.

9. **Fix R13-R24 (Medium/Low) NINTH** -- These improve robustness, security posture, and operational capability but don't block basic trading safety.

---

## Positive Observations

1. **Excellent VPS Hardening Script** (`scripts/harden_vps.sh`) -- This is production-quality: CIS-aligned, idempotent, dry-run support, comprehensive verification, proper backup/rollback, audit logging, sysctl tuning. The security team clearly knows what they're doing at the infrastructure layer.

2. **Proper Secret Management Pattern** -- `.env.example` documents all secrets clearly. `.gitignore` properly excludes `.env`, `secrets/`, and key files. `docker-compose.yml` uses `${VAR:?err}` syntax to fail fast on missing secrets. The secrets volume is mounted read-only.

3. **Decimal for Monetary Values** -- `StateManager`, `RiskEngine`, `PositionSizer`, and `PaperBroker` all use `Decimal` for prices, quantities, and P&L. This is correct and prevents float precision issues.

4. **Sound Risk Architecture on Paper** -- The Iron Rules are excellent: withdrawals off, IP whitelist, 90-day paper minimum, capital floor/hard cap, monthly kill-switch testing. The risk configuration YAML has proper conservative/experimental profiles with differentiated limits.

5. **Healthcheck Configuration** -- All Docker services have healthchecks defined with appropriate intervals and retries. The database services have startup condition dependencies.

6. **Multi-Stage Dashboard Dockerfile** -- `docker/Dockerfile.dashboard` uses proper multi-stage build with non-root runtime user. Good security practice.

7. **Comprehensive Test Fixtures** -- `tests/conftest.py` has well-designed fixtures for temp DB, mock configs, and sample candles. The test structure supports expansion.

8. **Strategy Parameterization** -- Both strategies (`EMA_Crossover`, `RSI_MACD_Combo`) have clean parameter systems with optimization ranges, making walk-forward analysis straightforward.

9. **News Sentiment Is Halt-Only** -- The sentiment filter correctly implements "halt-only" behavior (no trading signals from news) as specified. It cannot generate entry/exit signals, only halt trading -- a conservative and correct design choice.

10. **Paper/Live Mode Switch** -- `OrderManager` and `BinanceClient` both have `paper`/`testnet` flags, showing the intent for a clear paper-to-live transition path.

---

## Appendix: "3 AM Sunday" Scenario Walkthrough

| Time | Event | System Response | Actual Result |
|------|-------|----------------|---------------|
| 03:00 | BTC drops 20% in 10 min | RegimeDetector returns "volatile" | Correct |
| 03:01 | Bot A EMA crossover generates SELL | Signal created, position sizing computed | Correct |
| 03:02 | Daily loss limit exceeded | `check_daily_limit()` not called with real P&L | **No action** |
| 03:03 | Max drawdown (20%) exceeded | `check_max_drawdown()` never called | **No action** |
| 03:04 | `submit_order()` called | Validates size, returns `{"status": "submitted"}` | **Fake order, no exchange call** |
| 03:05 | `record_trade()` called | Writes to DuckDB | Recorded phantom trade |
| 03:06 | Kill switch SHOULD trigger | `is_triggered()`=False (never checked) | **No action** |
| 03:07 | Alerts SHOULD fire | No alert sender exists | **Silent failure** |
| 03:08 | Bot B generates SELL on same symbol | No cross-bot coordination | **Double exposure** |
| 03:09 | Operator wakes up, checks dashboard | Dashboard is a stub with title only | **No information** |
| 03:10 | Operator tries to kill switch via mobile | No kill switch button, no remote access | **Cannot act** |
| 03:30 | 30 minutes later | Bots continue "trading" on stale/corrupted state | **Cascading losses** |

**Root cause of every failure:** Incomplete wiring between safety components and the hot trading path.

---

*This review represents a static analysis of the codebase as of Week 1. Many findings are architectural/implementation gaps that can be addressed in subsequent sprints. The #1 action item: wire the safety mechanisms into the actual trading loop before any capital deployment.*

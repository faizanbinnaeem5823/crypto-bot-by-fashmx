# Crypto Trading Bot - Infrastructure & Operations Runbook

A production-hardened cryptocurrency trading bot built with **Freqtrade**, **Streamlit**, and comprehensive monitoring via **Grafana**, **Prometheus**, and **Loki**.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Environment Setup](#environment-setup)
5. [VPS Hardening](#vps-hardening)
6. [Data Download](#data-download)
7. [Running Tests](#running-tests)
8. [Starting the Dashboard](#starting-the-dashboard)
9. [Monitoring](#monitoring)
10. [Security Checklist](#security-checklist)
11. [Troubleshooting](#troubleshooting)
12. [Reference](#reference)

---

## Architecture Overview

```
                              +---------------------+
                              |    USER / ADMIN     |
                              +----------+----------+
                                         |
                              +----------v----------+
                              |  UFW Firewall       |
                              |  (Port 2299/8080/   |
                              |   8501/9090/3000)   |
                              +----------+----------+
                                         |
            +----------------------------+----------------------------+
            |                            |                            |
+-----------v-----------+   +------------v-----------+   +-----------v-----------+
| SSH (Port 2299)       |   | Freqtrade (Port 8080)  |   | Streamlit (Port 8501) |
| Key-based auth only   |   | REST API + Web UI      |   | Analytics Dashboard   |
| fail2ban protected    |   | Trading engine         |   | Real-time P&L         |
+-----------------------+   +------------+-----------+   +-----------------------+
                                         |
                              +----------v----------+
                              |   Docker Compose    |
                              |   (Internal Net)    |
                              +----------+----------+
                                         |
            +----------------------------+----------------------------+
            |                            |                            |
+-----------v-----------+   +------------v-----------+   +-----------v-----------+
| PostgreSQL (Port 5432)|   | Redis (Port 6379)      |   | Prometheus (Port    |
| Trade history, state  |   | Caching, rate limits   |   | 9090)                 |
| OHLCV data            |   | Session store          |   | Metrics collection    |
+-----------------------+   +------------------------+   +-----------+-----------+
                                                                       |
                                                          +------------v-----------+
                                                          | Grafana (Port 3000)    |
                                                          | Visualization &        |
                                                          | Alerting               |
                                                          +------------------------+
                                                          | Loki (Port 3100)       |
                                                          | Centralized logging    |
                                                          +------------------------+
```

### Component Descriptions

| Component | Purpose | Port | Bind Address |
|-----------|---------|------|--------------|
| SSH | Secure remote access | 2299 | 0.0.0.0 |
| Freqtrade | Trading engine & API | 8080 | 0.0.0.0 |
| Streamlit | Analytics dashboard | 8501 | 0.0.0.0 |
| Prometheus | Metrics collection | 9090 | 0.0.0.0 |
| Grafana | Visualization | 3000 | 0.0.0.0 |
| Loki | Log aggregation | 3100 | 127.0.0.1 |
| PostgreSQL | Database | 5432 | 127.0.0.1 |
| Redis | Cache | 6379 | 127.0.0.1 |

---

## Prerequisites

### Hardware Requirements

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| RAM | 4 GB | 8 GB | Trading + monitoring stack |
| Storage | 80 GB SSD | 160 GB SSD | Historical data grows over time |
| CPU | 2 vCPU | 4 vCPU | For parallel backtesting |
| Bandwidth | 1 TB/month | Unlimited | Exchange WebSocket data |

### Software Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Ubuntu | 22.04 LTS | Other versions may work but are not tested |
| Docker | 24.x+ | Installed by hardening script |
| Docker Compose | 2.x+ | Installed by hardening script |
| Domain Name | Optional | For TLS certificates via Let's Encrypt |

### External Requirements

- **Exchange Account**: Binance or Bybit account with API keys
- **API Key Permissions**: Read + Spot Trading (NO withdrawal permission)
- **VPS Provider**: Vultr Tokyo (or any VPS with <50ms latency to exchange)
- **SSH Key Pair**: Ed25519 or RSA 4096-bit key pair for server access

### Local Machine Setup

Generate an SSH key pair (if you don't have one):

```bash
# Generate Ed25519 key (recommended)
ssh-keygen -t ed25519 -C "cryptobot-$(date +%Y%m%d)" -f ~/.ssh/cryptobot_ed25519

# Or generate RSA 4096-bit key
ssh-keygen -t rsa -b 4096 -C "cryptobot-$(date +%Y%m%d)" -f ~/.ssh/cryptobot_rsa
```

---

## Quick Start

Complete setup from fresh Ubuntu 22.04 to running trading bot in 5 steps.

### Step 1: Clone the Repository

```bash
# On your LOCAL machine, clone the repository
git clone https://github.com/your-org/crypto-trading-bot.git
cd crypto-trading-bot
```

### Step 2: Run VPS Hardening

```bash
# Copy the hardening script to your VPS
scp -P 22 scripts/harden_vps.sh root@<YOUR_VPS_IP>:/root/

# SSH into the VPS as root
ssh root@<YOUR_VPS_IP>

# Run the hardening script
bash /root/harden_vps.sh

# The script will:
# - Create 'cryptobot' user with sudo access
# - Configure SSH on port 2299 (key-only)
# - Set up UFW firewall
# - Install and configure fail2ban
# - Tune kernel parameters for trading
# - Install and harden Docker
# - Configure time sync to Asia/Tokyo
# - Set up logging infrastructure
```

After hardening completes, copy your SSH key to the new user:

```bash
# On your LOCAL machine
ssh-copy-id -i ~/.ssh/cryptobot_ed25519.pub -p 2299 cryptobot@<YOUR_VPS_IP>

# Test the connection
ssh -p 2299 cryptobot@<YOUR_VPS_IP>
```

### Step 3: Configure Environment

```bash
# On the VPS as cryptobot user
cd ~
git clone https://github.com/your-org/crypto-trading-bot.git
cd crypto-trading-bot

# Copy example environment file
cp .env.example .env

# Edit .env with your API keys and settings
nano .env
```

### Step 4: Start the Stack

```bash
# Pull and start all services
docker compose pull
docker compose up -d

# Verify all containers are running
docker compose ps
```

### Step 5: Access the Interfaces

| Service | URL | Credentials |
|---------|-----|-------------|
| Freqtrade UI | `http://<VPS_IP>:8080` | From `.env` |
| Streamlit | `http://<VPS_IP>:8501` | None |
| Grafana | `http://<VPS_IP>:3000` | admin/admin |
| Prometheus | `http://<VPS_IP>:9090` | None |

---

## Environment Setup

The `.env` file contains all configuration for the trading bot. Never commit this file to git.

### Creating .env from .env.example

```bash
# Copy the template
cp .env.example .env

# Edit with your preferred editor
nano .env        # or vim, or any editor
```

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `EXCHANGE_NAME` | Exchange to trade on | `binance` or `bybit` |
| `EXCHANGE_KEY` | API Key from exchange | `AbCdEfGhIjKlMnOp` |
| `EXCHANGE_SECRET` | API Secret from exchange | `supersecretstring` |
| `FREQTRADE_USERNAME` | Freqtrade UI login | `admin` |
| `FREQTRADE_PASSWORD` | Freqtrade UI password | `StrongP@ssw0rd!` |
| `JWT_SECRET_KEY` | Random secret for tokens | `$(openssl rand -hex 32)` |
| `TELEGRAM_TOKEN` | Bot token for alerts (optional) | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID | `123456789` |
| `POSTGRES_PASSWORD` | Database password | `$(openssl rand -hex 16)` |
| `REDIS_PASSWORD` | Cache password | `$(openssl rand -hex 16)` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password | `SecureGrafana123!` |

### Generating Secure Secrets

```bash
# Generate a JWT secret
openssl rand -hex 32

# Generate database password
openssl rand -hex 16

# Generate Grafana password (use a memorable strong password)
# Recommended: 16+ chars with uppercase, lowercase, numbers, symbols
```

### .env Example

```bash
# =============================================================================
# Crypto Trading Bot - Environment Configuration
# ⚠️  NEVER COMMIT THIS FILE TO VERSION CONTROL
# =============================================================================

# ---- Exchange Configuration ----
EXCHANGE_NAME=binance
EXCHANGE_KEY=your_api_key_here
EXCHANGE_SECRET=your_api_secret_here
EXCHANGE_SANDBOX=false

# ---- Freqtrade Web UI ----
FREQTRADE_USERNAME=admin
FREQTRADE_PASSWORD=change_this_strong_password
JWT_SECRET_KEY=generate_with_openssl_rand_hex_32

# ---- Database ----
POSTGRES_USER=freqtrade
POSTGRES_PASSWORD=generate_with_openssl_rand_hex_16
POSTGRES_DB=trading

# ---- Cache ----
REDIS_PASSWORD=generate_with_openssl_rand_hex_16

# ---- Monitoring ----
GRAFANA_ADMIN_PASSWORD=change_this_strong_password_too

# ---- Notifications ----
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# ---- Trading Parameters ----
STAKE_AMOUNT=100
MAX_OPEN_TRADES=3
TIMEFRAME=5m

# ---- Feature Flags ----
DRY_RUN=true          # Set to false when ready to trade with real money
```

---

## VPS Hardening

The `scripts/harden_vps.sh` script implements defense-in-depth security following CIS benchmarks.

### Running the Script

```bash
# Review what the script will do (dry run)
root@vps:~# bash harden_vps.sh --dry-run

# Apply hardening
root@vps:~# bash harden_vps.sh
```

### What the Script Does

| # | Action | Security Standard |
|---|--------|-------------------|
| 1 | Creates `cryptobot` user with sudo, no password login | CIS 4.1.1 |
| 2 | SSH key-only auth on port 2299 | CIS 5.2.x |
| 3 | UFW firewall with explicit allowlist | CIS 3.5.x |
| 4 | fail2ban brute-force protection | CIS 4.5.x |
| 5 | Automatic security patches | CIS 1.9 |
| 6 | Kernel tuning for low-latency trading | NIST SP 800-53 |
| 7 | Docker hardening (userns, no-new-privs) | CIS Docker 4.1 |
| 8 | Time sync with Asia/Tokyo | Financial audit trail |
| 9 | Centralized logging + log rotation | CIS 4.2.x |
| 10 | Full verification report | NIST SP 800-53 AU-6 |

### Post-Hardening Checklist

After running the hardening script, verify these items:

- [ ] Can SSH as `cryptobot` user on port 2299
- [ ] Cannot SSH as root
- [ ] Cannot login with password (key only)
- [ ] UFW is active: `sudo ufw status verbose`
- [ ] fail2ban is running: `sudo systemctl status fail2ban`
- [ ] Docker containers start successfully
- [ ] All monitoring URLs are accessible

---

## Data Download

Historical data is required for backtesting and strategy optimization.

### Downloading Exchange Data

```bash
# Enter the Freqtrade container
docker compose exec freqtrade /bin/bash

# Download 1 year of 5-minute candlestick data for BTC/USDT
freqtrade download-data \
    --exchange binance \
    --pairs BTC/USDT \
    --timeframes 5m 1h 1d \
    --timerange 20230101-20240101

# Download multiple pairs
freqtrade download-data \
    --exchange binance \
    --pairs BTC/USDT ETH/USDT SOL/USDT BNB/USDT \
    --timeframes 5m 15m 1h 4h 1d \
    --timerange 20230101-20240101
```

### Download with Custom Configuration

```bash
# Using a config file for batch downloads
docker compose exec freqtrade freqtrade download-data \
    --config /freqtrade/user_data/config.json \
    --timerange 20220101-20240101 \
    --timeframes 5m 1h 1d
```

### Data Storage

Downloaded data is stored in a Docker volume:

```bash
# Check data location
docker compose exec freqtrade ls -la /freqtrade/user_data/data/

# Data is persisted in the Docker volume 'freqtrade_user_data'
# It survives container restarts and rebuilds
```

### Updating Data

```bash
# Freqtrade will download only missing data when you specify
# a timerange extending beyond current data
freqtrade download-data \
    --exchange binance \
    --pairs BTC/USDT \
    --timeframes 5m \
    --timerange 20240101-
```

---

## Running Tests

The project uses `pytest` for testing strategies and utilities.

### Running All Tests

```bash
# On the VPS (inside project directory)
cd ~/crypto-trading-bot

# Run all tests
docker compose exec freqtrade pytest /freqtrade/user_data/tests/ -v

# Or locally if you have Python environment
python -m pytest tests/ -v
```

### Running Specific Test Categories

```bash
# Strategy tests only
pytest tests/strategies/ -v

# Utility tests
pytest tests/utils/ -v

# Integration tests (require exchange API)
pytest tests/integration/ -v --exchange binance

# With coverage report
pytest tests/ -v --cov=strategies --cov-report=html
```

### Running a Single Test

```bash
# Run specific test file
pytest tests/strategies/test_custom_strategy.py -v

# Run specific test function
pytest tests/strategies/test_custom_strategy.py::test_entry_signal -v

# Run with debug logging
pytest tests/ -v --log-cli-level=DEBUG
```

### Backtest a Strategy

```bash
# Enter container
docker compose exec freqtrade /bin/bash

# Run backtest
freqtrade backtesting \
    --strategy CustomStrategy \
    --pairs BTC/USDT \
    --timerange 20230101-20240101 \
    --timeframe 5m \
    --config /freqtrade/user_data/config.json

# Export results to CSV
freqtrade backtesting \
    --strategy CustomStrategy \
    --pairs BTC/USDT \
    --timerange 20230101-20240101 \
    --timeframe 5m \
    --export trades \
    --config /freqtrade/user_data/config.json
```

---

## Starting the Dashboard

The Streamlit dashboard provides real-time analytics and trade visualization.

### Using Docker Compose (Recommended)

```bash
# Start only the dashboard
docker compose up -d streamlit

# View logs
docker compose logs -f streamlit
```

### Running Locally for Development

```bash
# Install dependencies
pip install -r requirements-dashboard.txt

# Run Streamlit
streamlit run dashboard/app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true
```

### Dashboard Features

| Feature | Description | URL Path |
|---------|-------------|----------|
| Portfolio | Current balance, P&L, allocation | `/` |
| Active Trades | Open positions with real-time data | `/Active_Trades` |
| Trade History | Completed trades with analysis | `/Trade_History` |
| Performance | Strategy performance metrics | `/Performance` |
| Market Overview | Price charts and indicators | `/Market_Overview` |

---

## Monitoring

### Grafana Dashboards

Access Grafana at `http://<VPS_IP>:3000`

| Dashboard | Purpose | Default URL |
|-----------|---------|-------------|
| System Overview | CPU, memory, disk, network | `http://<VPS_IP>:3000/d/system` |
| Trading Performance | P&L, win rate, drawdown | `http://<VPS_IP>:3000/d/trading` |
| Exchange Health | API latency, rate limits | `http://<VPS_IP>:3000/d/exchange` |
| Docker Metrics | Container resource usage | `http://<VPS_IP>:3000/d/docker` |

### Default Login

- **Username**: `admin`
- **Password**: Set in `.env` (`GRAFANA_ADMIN_PASSWORD`)

**⚠️ Security Warning**: Change the default password immediately after first login.

### Prometheus Queries

```promql
# Current bot uptime
up{job="freqtrade"}

# Number of open trades
freqtrade_open_trades_count

# Total profit/loss in USD
freqtrade_profit_total_usd

# Exchange API latency
freqtrade_api_call_duration_seconds_bucket

# Database connection pool
pg_stat_activity_count{state="active"}
```

### Reading Logs

```bash
# View all container logs
docker compose logs -f

# View specific service logs
docker compose logs -f freqtrade
docker compose logs -f streamlit

# View historical logs via Loki in Grafana
# Navigate to Explore -> Loki -> Select app="freqtrade"

# View system security logs
sudo tail -f /var/log/auth.log

# View fail2ban status
sudo fail2ban-client status sshd

# View hardening log
sudo tail -f /var/log/cryptobot/hardening.log
```

### Alert Configuration

Grafana alerts can be configured for:

- **High CPU Usage** > 80% for 5 minutes
- **Memory Usage** > 90% for 2 minutes
- **Disk Space** > 85% full
- **Bot Down** - freqtrade container not running
- **API Errors** - Exchange API error rate > 5%
- **Open Trade Timeout** - Trade open longer than expected

---

## Security Checklist

This checklist must be completed before trading with real funds.

### API Key Security

- [ ] **Withdrawal permission is DISABLED** on exchange API key
- [ ] API key is restricted to the VPS IP address (IP whitelisting)

```
⚠️  CRITICAL: Verify the API key CANNOT withdraw funds.
   Binance: API Management -> Edit -> Enable Reading & Spot Trading ONLY
   Bybit: API Management -> Edit -> Spot Trading only, NO withdrawal
```

- [ ] API key permissions: `READ` + `SPOT TRADING` only
- [ ] IP whitelist configured on exchange (your VPS IP)
- [ ] Test API key works with `DRY_RUN=true` first

### Server Security

- [ ] VPS hardening script executed successfully
- [ ] SSH key-based authentication working (no password)
- [ ] Root login disabled
- [ ] UFW firewall active with correct rules
- [ ] fail2ban running and protecting SSH
- [ ] Automatic security updates enabled
- [ ] `.env` file has `chmod 600` permissions
- [ ] `.env` file is NOT in git (`git check-ignore -v .env`)

### Trading Safety

- [ ] Start with `DRY_RUN=true` for at least 1 week
- [ ] Set `STAKE_AMOUNT` to small value (e.g., $10) for initial live trading
- [ ] Set `MAX_OPEN_TRADES=1` initially
- [ ] Enable Telegram notifications for all trades
- [ ] Configure stop-loss on every strategy (max 5% loss per trade)
- [ ] Test emergency stop: `docker compose stop freqtrade`

### Data Protection

- [ ] Database password is strong and unique
- [ ] Grafana admin password changed from default
- [ ] JWT secret is randomly generated
- [ ] No API keys committed to git
- [ ] No passwords in shell history (`history -c` after editing .env)

### Backup Checklist

- [ ] Strategy configurations backed up
- [ ] `.env` file backed up securely (password manager)
- [ ] Database backup script configured
- [ ] Docker volumes backed up

---

## Troubleshooting

### Cannot SSH After Hardening

**Symptom**: Connection refused or timeout after running hardening script

**Solutions**:
```bash
# 1. Check if sshd is running on the new port
sudo systemctl status sshd

# 2. Verify port is listening
sudo ss -tlnp | grep 2299

# 3. Check UFW allowed the port
sudo ufw status | grep 2299

# 4. If completely locked out, use Vultr console (web-based)
#    Login as root via console, then:
sudo ufw allow 2299/tcp
sudo systemctl restart sshd
```

### Docker Containers Fail to Start

**Symptom**: `docker compose up` shows errors

**Solutions**:
```bash
# Check container logs
docker compose logs --tail=50 freqtrade

# Common issues:
# 1. Port conflict - check what's using the port
sudo ss -tlnp | grep 8080

# 2. Missing .env file
cp .env.example .env && nano .env

# 3. Permission issues on volumes
sudo chown -R 1000:1000 ./user_data/

# 4. Reset everything
docker compose down -v
docker compose up -d
```

### Freqtrade API Connection Refused

**Symptom**: Cannot access `http://<IP>:8080`

**Solutions**:
```bash
# 1. Check if container is running
docker compose ps

# 2. Check Freqtrade logs
docker compose logs --tail=100 freqtrade

# 3. Verify API configuration in config.json
cat user_data/config.json | grep -A5 "api_server"

# 4. Check if UFW allows port 8080
sudo ufw status | grep 8080
```

### Database Connection Errors

**Symptom**: Freqtrade shows PostgreSQL connection errors

**Solutions**:
```bash
# 1. Check PostgreSQL container
docker compose ps postgres

# 2. Check PostgreSQL logs
docker compose logs postgres

# 3. Verify credentials match between .env and config
# POSTGRES_PASSWORD in .env must match password in config.json

# 4. Test connection manually
docker compose exec postgres psql -U freqtrade -d trading -c "SELECT 1;"
```

### High Memory Usage

**Symptom**: Server becomes slow or OOM killer triggers

**Solutions**:
```bash
# 1. Check memory usage
free -h
docker stats --no-stream

# 2. Reduce PostgreSQL memory
# Edit docker-compose.yml, add to postgres service:
#   shm_size: '256mb'

# 3. Limit container memory in docker-compose.yml:
# deploy:
#   resources:
#     limits:
#       memory: 512M

# 4. Reduce swap usage (already set to 10 by hardening)
sysctl vm.swappiness

# 5. Restart with memory limits
docker compose up -d --force-recreate
```

### Time Synchronization Issues

**Symptom**: Exchange API rejects requests with timestamp errors

**Solutions**:
```bash
# 1. Check current time
date && timedatectl

# 2. Force time sync
sudo chronyc makestep

# 3. Check chrony status
chronyc tracking

# 4. Restart chrony if needed
sudo systemctl restart chronyd
```

### Grafana Login Fails

**Symptom**: Cannot login to Grafana with admin/admin

**Solutions**:
```bash
# 1. Reset Grafana password
docker compose exec grafana grafana-cli admin reset-admin-password <new_password>

# 2. Check Grafana logs
docker compose logs grafana

# 3. Verify database is accessible
docker compose exec postgres psql -U grafana -c "SELECT 1;"
```

### Strategy Not Found

**Symptom**: Freqtrade reports strategy not found

**Solutions**:
```bash
# 1. Check strategy file location
ls -la user_data/strategies/

# 2. Verify strategy class name matches filename
# File: custom_strategy.py, Class: CustomStrategy

# 3. Check Freqtrade can see it
docker compose exec freqtrade freqtrade list-strategies

# 4. Ensure proper permissions
chmod 644 user_data/strategies/*.py
```

### Log Files Growing Too Large

**Symptom**: Disk space warning, /var/log filling up

**Solutions**:
```bash
# 1. Check disk usage
df -h

# 2. Check log sizes
du -sh /var/log/cryptobot/*
du -sh /var/lib/docker/containers/*/*-json.log

# 3. Force log rotation
sudo logrotate -f /etc/logrotate.d/cryptobot

# 4. Clear Docker container logs
docker compose logs --tail=100 freqtrade > /dev/null
sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log

# 5. Run Docker system prune (removes unused images/volumes)
docker system prune -a --volumes
```

### Getting Support

If issues persist:

1. Check logs: `docker compose logs --tail=200 > support.log`
2. Check hardening log: `sudo cat /var/log/cryptobot/hardening.log`
3. Run verification: `sudo bash scripts/harden_vps.sh --dry-run`
4. Open an issue with logs and reproduction steps

---

## Reference

### Useful Commands

```bash
# ---- Container Management ----
docker compose up -d              # Start all services
docker compose down               # Stop all services
docker compose restart freqtrade  # Restart single service
docker compose ps                 # List running containers
docker compose logs -f            # Follow all logs

# ---- Freqtrade Operations ----
docker compose exec freqtrade freqtrade trade --strategy CustomStrategy
docker compose exec freqtrade freqtrade backtesting --strategy CustomStrategy
docker compose exec freqtrade freqtrade download-data --pairs BTC/USDT --timeframes 5m

# ---- System Monitoring ----
htop                              # Interactive process viewer
iotop                             # Disk I/O monitoring
nethogs                           # Per-process network usage
ss -tlnp                          # Listening ports with processes

# ---- Security ----
sudo ufw status verbose           # Firewall status
sudo fail2ban-client status sshd  # fail2ban status
sudo tail -f /var/log/auth.log    # Authentication logs
sudo lastb                        # Failed login attempts

# ---- Database ----
docker compose exec postgres psql -U freqtrade -d trading
docker compose exec redis redis-cli -a <REDIS_PASSWORD> info
```

### File Locations

| File | Path | Description |
|------|------|-------------|
| Hardening log | `/var/log/cryptobot/hardening.log` | VPS hardening output |
| App logs | `/var/log/cryptobot/app.log` | Application logs |
| SSH config | `/etc/ssh/sshd_config.d/99-hardening.conf` | SSH hardening |
| Firewall | `/etc/ufw/` | UFW configuration |
| fail2ban | `/etc/fail2ban/jail.local` | Intrusion prevention |
| sysctl | `/etc/sysctl.d/99-crypto-trading.conf` | Kernel tuning |
| Docker | `/etc/docker/daemon.json` | Docker daemon config |
| Backups | `/root/hardening-backups/` | Config backups |

### Security Standards Reference

| Standard | Description |
|----------|-------------|
| CIS Ubuntu 22.04 | Center for Internet Security Benchmark |
| NIST SP 800-53 | Security and Privacy Controls for Federal Information Systems |
| PCI DSS | Payment Card Industry Data Security Standard (relevant for financial apps) |
| ISO 27001 | Information Security Management Systems |

---

*Last updated: $(date +%Y-%m-%d). For updates and issues, see the project repository.*

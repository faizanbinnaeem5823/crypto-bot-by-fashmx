# CryptoBot Backtest Report

**Generated:** 2026-05-20 00:24 UTC
**Initial Capital:** $500
**Trading Fees:** 0.1% | **Slippage:** 0.05%

---

## 1. Executive Summary

This report compares the EMA Crossover and RSI+MACD Combo strategies
across six timeframes (1m, 5m, 15m, 1h, 4h, 1d) using realistic fee assumptions.

**Best Overall (Sharpe):** `1d` timeframe | RSI_MACD on ETH/USDT
- Sharpe Ratio: **1.14**
- Total Return: **1341.6%**
- Max Drawdown: **-48.2%**

**Best Overall (Return):** `1d` timeframe | RSI_MACD on ETH/USDT
- Total Return: **1341.6%**
- Max Drawdown: **-48.2%**

---

## 2. Performance by Timeframe

Average metrics across all symbols and strategies per timeframe:

| timeframe   |   Avg Return % |   Avg Sharpe |   Avg Max DD % |   Avg Win Rate % |   Avg Profit Factor |   Avg Calmar |   Avg Trades |
|:------------|---------------:|-------------:|---------------:|-----------------:|--------------------:|-------------:|-------------:|
| 1m          |        -100.00 |        -0.42 |        -100.00 |            39.70 |                0.84 |        -1.00 |     27808.75 |
| 5m          |        -100.00 |        -0.40 |        -100.00 |            42.62 |                0.78 |        -1.00 |      6898.75 |
| 15m         |         -97.93 |        -0.21 |         -99.24 |            44.75 |                0.94 |        -0.99 |      2429.50 |
| 1h          |           4.58 |        -0.04 |         -86.83 |            47.24 |                1.19 |         0.21 |       637.75 |
| 4h          |         190.44 |         0.20 |         -73.62 |            50.13 |                2.00 |         3.18 |       160.00 |
| 1d          |         822.56 |         0.96 |         -48.32 |            68.01 |                1.09 |        17.13 |        27.00 |

---

## 3. Detailed Results by Symbol

### BTC/USDT

| Timeframe   | Strategy   |   Return % |   Sharpe |   Max DD % |   Win Rate % |   Profit Factor |   Calmar |   Trades |   Final Equity ($) |
|:------------|:-----------|-----------:|---------:|-----------:|-------------:|----------------:|---------:|---------:|-------------------:|
| 1m          | EMA_Cross  |    -100.00 |    -0.25 |    -100.00 |        27.15 |            1.62 |    -1.00 |    49318 |               0.00 |
| 1m          | RSI_MACD   |    -100.00 |    -0.62 |    -100.00 |        51.93 |            0.14 |    -1.00 |     4668 |               0.00 |
| 5m          | EMA_Cross  |    -100.00 |    -0.46 |    -100.00 |        27.54 |            1.03 |    -1.00 |    12306 |               0.00 |
| 5m          | RSI_MACD   |    -100.00 |    -0.40 |    -100.00 |        57.41 |            0.51 |    -1.00 |     1350 |               0.00 |
| 15m         | EMA_Cross  |    -100.00 |    -0.36 |    -100.00 |        27.08 |            0.95 |    -1.00 |     4339 |               0.01 |
| 15m         | RSI_MACD   |     -97.52 |    -0.11 |     -98.90 |        61.85 |            0.87 |    -0.99 |      498 |              12.42 |
| 1h          | EMA_Cross  |     -97.20 |    -0.19 |     -98.28 |        27.22 |            0.97 |    -0.99 |     1139 |              14.01 |
| 1h          | RSI_MACD   |      87.37 |     0.10 |     -74.86 |        67.42 |            1.37 |     1.17 |      132 |             936.83 |
| 4h          | EMA_Cross  |     -40.96 |     0.04 |     -85.48 |        27.65 |            1.14 |    -0.48 |      293 |             295.18 |
| 4h          | RSI_MACD   |     320.61 |     0.31 |     -59.30 |        72.41 |            2.68 |     5.41 |       29 |            2103.07 |
| 1d          | EMA_Cross  |     337.74 |     0.76 |     -48.27 |        35.29 |            2.00 |     7.00 |       51 |            2188.72 |
| 1d          | RSI_MACD   |     871.59 |     1.04 |     -44.62 |       100.00 |            0.00 |    19.53 |        4 |            4857.94 |

**Best for BTC/USDT:** 1d RSI_MACD | Sharpe: 1.04 | Return: 871.6%

### ETH/USDT

| Timeframe   | Strategy   |   Return % |   Sharpe |   Max DD % |   Win Rate % |   Profit Factor |   Calmar |   Trades |   Final Equity ($) |
|:------------|:-----------|-----------:|---------:|-----------:|-------------:|----------------:|---------:|---------:|-------------------:|
| 1m          | EMA_Cross  |    -100.00 |    -0.29 |    -100.00 |        27.16 |            1.44 |    -1.00 |    52221 |               0.00 |
| 1m          | RSI_MACD   |    -100.00 |    -0.53 |    -100.00 |        52.55 |            0.17 |    -1.00 |     5028 |               0.00 |
| 5m          | EMA_Cross  |    -100.00 |    -0.44 |    -100.00 |        27.49 |            1.00 |    -1.00 |    12545 |               0.00 |
| 5m          | RSI_MACD   |    -100.00 |    -0.31 |    -100.00 |        58.03 |            0.59 |    -1.00 |     1394 |               0.00 |
| 15m         | EMA_Cross  |    -100.00 |    -0.32 |    -100.00 |        27.49 |            0.96 |    -1.00 |     4376 |               0.01 |
| 15m         | RSI_MACD   |     -94.21 |    -0.06 |     -98.07 |        62.57 |            0.96 |    -0.96 |      505 |              28.93 |
| 1h          | EMA_Cross  |     -97.35 |    -0.16 |     -98.49 |        27.39 |            0.99 |    -0.99 |     1150 |              13.25 |
| 1h          | RSI_MACD   |     125.50 |     0.11 |     -75.71 |        66.92 |            1.41 |     1.66 |      130 |            1127.48 |
| 4h          | EMA_Cross  |     -26.92 |     0.09 |     -86.74 |        28.03 |            1.19 |    -0.31 |      289 |             365.39 |
| 4h          | RSI_MACD   |     509.03 |     0.35 |     -62.97 |        72.41 |            2.98 |     8.08 |       29 |            3045.16 |
| 1d          | EMA_Cross  |     739.25 |     0.92 |     -52.17 |        36.73 |            2.38 |    14.17 |       49 |            4196.25 |
| 1d          | RSI_MACD   |    1341.65 |     1.14 |     -48.20 |       100.00 |            0.00 |    27.83 |        4 |            7208.24 |

**Best for ETH/USDT:** 1d RSI_MACD | Sharpe: 1.14 | Return: 1341.6%

---

## 4. Strategy Comparison

| strategy   |   Avg Return % |   Avg Sharpe |   Avg Max DD % |   Avg Win Rate % |   Avg Profit Factor |   Avg Trades |
|:-----------|---------------:|-------------:|---------------:|-----------------:|--------------------:|-------------:|
| EMA_Cross  |          17.88 |        -0.06 |         -89.12 |            28.85 |                1.31 |     11506.33 |
| RSI_MACD   |         222.00 |         0.09 |         -80.22 |            68.63 |                0.97 |      1147.58 |

---

## 5. Recommendations

### Conservative Pick (Bot A - 15m to 4h): **1d**

| Parameter | Value |
|-----------|-------|
| Timeframe | `1d` |
| Strategy | RSI_MACD |
| Symbol | ETH/USDT |
| Sharpe Ratio | 1.14 |
| Total Return | 1341.6% |
| Max Drawdown | -48.2% |
| Win Rate | 100.0% |
| Profit Factor | 0.00 |
| Total Trades | 4 |
| Final Equity | $7208.24 |

**Why this pick:** Balanced trade frequency with strong risk-adjusted returns.
Suitable for 24/7 automated operation with moderate capital ($500).

### Long-term Pick (1d timeframe): **RSI_MACD**

| Parameter | Value |
|-----------|-------|
| Timeframe | `1d` |
| Strategy | RSI_MACD |
| Symbol | ETH/USDT |
| Sharpe Ratio | 1.14 |
| Total Return | 1341.6% |
| Max Drawdown | -48.2% |
| Total Trades | 4 |

**Why this pick:** Minimal fees, cleanest signals, set-and-forget operation.

---

## 6. Timeframe Selection Guide

Use this table to choose the right timeframe for your trading style:

| Timeframe | Category | Best For | Pros | Cons | Verdict |
|-----------|----------|----------|------|------|---------|
| **1m** | Scalping | High-frequency bots | Most signals | High fee drag, extreme noise | Avoid at $500 capital |
| **5m** | Day Trading | Active day trading | Good frequency | Fee drag significant | Bot B only |
| **15m** | Swing Trading | Balanced approach | Balanced risk/reward | Moderate fees | Bot A primary / Bot B |
| **1h** | Swing Trading | Trend following | Lower fees, cleaner signals | Fewer trades | Bot A good option |
| **4h** | Position Trading | Trend capture | Low fee impact, strong signals | Infrequent | Bot A primary |
| **1d** | Long-term | Set-and-forget | Minimal fees, best Sharpe | Very few trades | Conservative fallback |

### Key Insights

1. **Lower timeframes (1m-5m)** generate more signals but suffer from fee drag.
   At 0.1% fee per trade, a round-trip costs 0.2% - aggressive strategies need
   >0.3% average profit per trade just to break even.

2. **Mid timeframes (15m-1h)** offer the best balance of signal quality and
   trade frequency. Recommended for Bot A with $500 capital.

3. **Higher timeframes (4h-1d)** have minimal fee impact and the cleanest signals,
   but require patience. Best for conservative/risk-averse operators.

4. **EMA Crossover** tends to perform better in trending markets (crypto bull runs).

5. **RSI+MACD Combo** filters for higher-confidence entries, reducing whipsaws
   in choppy/sideways markets.

---

## 7. Complete Results Table

All results sorted by Sharpe ratio (descending):

| symbol   | timeframe   | strategy   |   total_return_pct |   sharpe_ratio |   max_drawdown_pct |   win_rate_pct |   profit_factor |   calmar_ratio |   total_trades |   final_equity |
|:---------|:------------|:-----------|-------------------:|---------------:|-------------------:|---------------:|----------------:|---------------:|---------------:|---------------:|
| ETH/USDT | 1d          | RSI_MACD   |            1341.65 |           1.14 |             -48.20 |         100.00 |            0.00 |          27.83 |              4 |        7208.24 |
| BTC/USDT | 1d          | RSI_MACD   |             871.59 |           1.04 |             -44.62 |         100.00 |            0.00 |          19.53 |              4 |        4857.94 |
| ETH/USDT | 1d          | EMA_Cross  |             739.25 |           0.92 |             -52.17 |          36.73 |            2.38 |          14.17 |             49 |        4196.25 |
| BTC/USDT | 1d          | EMA_Cross  |             337.74 |           0.76 |             -48.27 |          35.29 |            2.00 |           7.00 |             51 |        2188.72 |
| ETH/USDT | 4h          | RSI_MACD   |             509.03 |           0.35 |             -62.97 |          72.41 |            2.98 |           8.08 |             29 |        3045.16 |
| BTC/USDT | 4h          | RSI_MACD   |             320.61 |           0.31 |             -59.30 |          72.41 |            2.68 |           5.41 |             29 |        2103.07 |
| ETH/USDT | 1h          | RSI_MACD   |             125.50 |           0.11 |             -75.71 |          66.92 |            1.41 |           1.66 |            130 |        1127.48 |
| BTC/USDT | 1h          | RSI_MACD   |              87.37 |           0.10 |             -74.86 |          67.42 |            1.37 |           1.17 |            132 |         936.83 |
| ETH/USDT | 4h          | EMA_Cross  |             -26.92 |           0.09 |             -86.74 |          28.03 |            1.19 |          -0.31 |            289 |         365.39 |
| BTC/USDT | 4h          | EMA_Cross  |             -40.96 |           0.04 |             -85.48 |          27.65 |            1.14 |          -0.48 |            293 |         295.18 |
| ETH/USDT | 15m         | RSI_MACD   |             -94.21 |          -0.06 |             -98.07 |          62.57 |            0.96 |          -0.96 |            505 |          28.93 |
| BTC/USDT | 15m         | RSI_MACD   |             -97.52 |          -0.11 |             -98.90 |          61.85 |            0.87 |          -0.99 |            498 |          12.42 |
| ETH/USDT | 1h          | EMA_Cross  |             -97.35 |          -0.16 |             -98.49 |          27.39 |            0.99 |          -0.99 |           1150 |          13.25 |
| BTC/USDT | 1h          | EMA_Cross  |             -97.20 |          -0.19 |             -98.28 |          27.22 |            0.97 |          -0.99 |           1139 |          14.01 |
| BTC/USDT | 1m          | EMA_Cross  |            -100.00 |          -0.25 |            -100.00 |          27.15 |            1.62 |          -1.00 |          49318 |           0.00 |
| ETH/USDT | 1m          | EMA_Cross  |            -100.00 |          -0.29 |            -100.00 |          27.16 |            1.44 |          -1.00 |          52221 |           0.00 |
| ETH/USDT | 5m          | RSI_MACD   |            -100.00 |          -0.31 |            -100.00 |          58.03 |            0.59 |          -1.00 |           1394 |           0.00 |
| ETH/USDT | 15m         | EMA_Cross  |            -100.00 |          -0.32 |            -100.00 |          27.49 |            0.96 |          -1.00 |           4376 |           0.01 |
| BTC/USDT | 15m         | EMA_Cross  |            -100.00 |          -0.36 |            -100.00 |          27.08 |            0.95 |          -1.00 |           4339 |           0.01 |
| BTC/USDT | 5m          | RSI_MACD   |            -100.00 |          -0.40 |            -100.00 |          57.41 |            0.51 |          -1.00 |           1350 |           0.00 |
| ETH/USDT | 5m          | EMA_Cross  |            -100.00 |          -0.44 |            -100.00 |          27.49 |            1.00 |          -1.00 |          12545 |           0.00 |
| BTC/USDT | 5m          | EMA_Cross  |            -100.00 |          -0.46 |            -100.00 |          27.54 |            1.03 |          -1.00 |          12306 |           0.00 |
| ETH/USDT | 1m          | RSI_MACD   |            -100.00 |          -0.53 |            -100.00 |          52.55 |            0.17 |          -1.00 |           5028 |           0.00 |
| BTC/USDT | 1m          | RSI_MACD   |            -100.00 |          -0.62 |            -100.00 |          51.93 |            0.14 |          -1.00 |           4668 |           0.00 |

---

## Disclaimer

> **Past performance does not guarantee future results.**
> This backtest uses synthetic OHLCV data when real historical data is unavailable.
> Synthetic data is calibrated to exhibit crypto-like volatility (~75% annualized)
> and regime-switching behavior, but cannot perfectly replicate real market conditions.
>
> **Always paper trade for at least 2-4 weeks before live deployment.**
> **Never risk more capital than you can afford to lose.**

*Report generated by CryptoBot Backtest Engine v1.0 | 2026-05-20 00:24 UTC*

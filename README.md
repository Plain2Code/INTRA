<div align="center">

# INTRA

### Intraday Noise Boundary Momentum Bot

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Capital.com](https://img.shields.io/badge/Capital.com-API-00D09C)](https://open-api.capital.com)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Automated intraday CFD trading bot for index CFDs on Capital.com. Fully mechanical entries, adaptive risk management, and a real-time dashboard.

</div>

---

## How It Works

Every trading day, a **noise zone** is computed around the daily opening price using the average absolute daily return of the last 14 days. While price stays inside this zone, no trade is taken. When price breaks beyond the boundary at :00 or :30, a position is opened in the breakout direction. All positions close before session end.

Each breakout is scored from 0.0 to 1.0 based on four confirmation factors:

| Factor | Weight | What it measures |
|--------|--------|------------------|
| Breakout strength | 0.40 | Distance beyond boundary in ATR units |
| Volume confirmation | 0.30 | Current volume vs 20-period average |
| MACD histogram | 0.15 | Momentum alignment with breakout direction |
| ADX | 0.15 | Trend strength favoring sustained breakout |

Minimum confidence to trade: 50%.

---

## Modules

The system is split into independent modules, wired together by the orchestrator.

### Core

| Module | Responsibility |
|--------|---------------|
| `capital_client` | Capital.com REST + WebSocket with auto-reconnect |
| `data_feed` | Candle buffers for 1min, 15min, and daily timeframes |
| `feature_engine` | Technical indicators (EMA, ATR, RSI, MACD, Bollinger, ADX) and noise boundary computation |
| `statistics` | EV calculation, Kelly criterion, correlation matrix, adaptive parameters |
| `news_filter` | Economic calendar blackout periods |

### Pipeline

| Module | Responsibility |
|--------|---------------|
| `risk_constraints` | Pre-trade safety checks: session times, spread, margin, news, cooldowns, position limits |
| `setup_engine` | Noise Breakout detection with confidence scoring |
| `trade_validator` | ATR-based SL computation, spread filter, EV gate (post-bootstrap) |
| `regime_classifier` | Volatility regime tagging (low / normal / high / extreme) |

### Execution

| Module | Responsibility |
|--------|---------------|
| `risk_manager` | Half-Kelly position sizing with correlation-adjusted exposure |
| `order_executor` | Order lifecycle (open, modify, close) |
| `trade_tracker` | Trade recording, feeds StatisticsEngine |
| `state_manager` | Daily P&L tracking, circuit breakers, kill switch |

---

## Orchestrator Pipeline

Every 1min candle triggers a four-layer pipeline per instrument. Any layer can veto.

**Layer 1: Risk Constraints** check session times, spread, margin, news blackout, cooldowns, and position limits. If anything fails, the trade is blocked before signal detection runs.

**Layer 2: Signal Detection** checks at :00 and :30 whether price has broken the noise boundary. Scores confidence from the four factors above. Needs at least 50% to proceed.

**Layer 3: Trade Validator** computes an ATR-based stop loss (1.5x ATR on 15min, adjusted for kurtosis and volatility). SL must be at least 3x the current spread. After 20 trades, the EV gate blocks setups with negative expected value.

**Layer 4: Position Sizing** uses Half-Kelly from accumulated statistics, capped at 3% risk and 3x leverage. Correlation-adjusted exposure prevents hidden concentration across correlated instruments.

---

## Exit Management

Three exits only. No breakeven stage, no time-based exits.

**Stop Loss** is set at 1.5x ATR(15min), adjusted for kurtosis and volatility. Managed broker-side, executes even if the bot disconnects.

**Trailing Stop** activates when profit reaches 0.75R. Trail distance is the larger of 1.2x ATR or the stats-derived optimal distance. Lets winners run while locking in gains.

**EOD Close** fires 5 minutes before session end. All positions are closed. No overnight risk.

---

## Risk Constraints

| Check | Threshold |
|-------|-----------|
| Daily loss limit | 3% of balance from daily peak |
| Position per instrument | Max 1 |
| Simultaneous positions | 4 hard cap |
| Correlated exposure | 2.5 effective positions |
| Available margin | Min 20% of equity |
| Spread filter | Blocked if spread > 1.5x average |
| Session buffers | No trades first 30min or last 30min |
| News blackout | 15 min before high-impact events |
| Consecutive SL hits | 3 per instrument pauses it |
| Cooldowns | 60s global, 10min per signal/instrument |

---

## Instruments

| Instrument | Epic | Session (UTC) |
|------------|------|---------------|
| DAX 40 | DE40 | 00:00 - 21:00 |
| CAC 40 | FR40 | 00:00 - 21:00 |
| NASDAQ 100 | US100 | 00:00 - 21:15 |
| S&P 500 | US500 | 00:00 - 21:15 |
| FTSE 100 | UK100 | 00:00 - 21:00 |

Each instrument runs independently with its own signal state, position, and circuit breakers.

---

## Adaptive Statistics

All trading parameters adapt as trade data accumulates:

| Parameter | Source | Fallback |
|-----------|--------|----------|
| Risk per trade | Half-Kelly fraction | 1.0% during bootstrap |
| Trailing distance | 30th percentile of win distribution | 1.2x ATR |
| EV gate | WR x avg_win - (1-WR) x avg_loss | Disabled until 20 trades |

Statistics are tracked per setup type and per instrument. Persisted in `stats.json`, trades in `trades.json`.

---

## References

- Zarattini, C., Aziz, A., & Barbon, A. (2024). *Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)*. Swiss Finance Institute. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172)
- Maroy, A. (2025). *Improvements to Intraday Momentum Strategies Using Parameter Optimization and Different Exit Strategies*. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5095349)
- Independent replication on ES/NQ futures: [Quantitativo](https://www.quantitativo.com/p/intraday-momentum-for-es-and-nq)

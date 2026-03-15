<div align="center">

# INTRA

### Intraday Noise Boundary & Momentum Trading Bot

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Capital.com](https://img.shields.io/badge/Capital.com-API-00D09C)](https://open-api.capital.com)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Automated intraday CFD trading bot for index CFDs on Capital.com. Two complementary strategies, fully mechanical entries, adaptive risk management, and a real-time dashboard.

</div>

---

## Strategies

### Noise Breakout (NB)

Based on Zarattini, Aziz & Barbon (2024). A **noise zone** is computed around the daily opening price using the average absolute daily return of the last 90 trading days. While price stays inside this zone, no trade is taken. When price breaks beyond the boundary at :00 or :30, a position is opened in the breakout direction.

Each breakout is scored from 0.0 to 1.0 based on four confirmation factors:

| Factor | Weight | What it measures |
|--------|--------|------------------|
| Breakout strength | 0.40 | Distance beyond boundary in ATR units |
| Volume confirmation | 0.30 | Current volume vs 20-period average |
| MACD histogram | 0.15 | Momentum alignment with breakout direction |
| ADX | 0.15 | Trend strength favoring sustained breakout |

The confidence score is informational only — all valid breakouts are traded (paper-conformant, purely mechanical).

NB trades stop 60 minutes before session close, handing over to Gao Momentum.

### Gao Momentum

Based on Gao, Han, Li & Zhou (2018). The last 30 minutes of a trading session tend to continue in the direction of the day's return — a documented intraday momentum effect.

30 minutes before session close, a single check is performed per instrument. The trading window is 25 minutes (30 to 5 min before close) — the last 5 minutes are reserved for EOD position closure:

- **Condition:** Has price moved ≥ 0.1% from previous daily close?
- **Direction:** Same as the intraday return (positive → LONG, negative → SHORT)
- **One-shot:** Fires once per asset per day. If the threshold isn't met, no trade.

If an NB position is still open, it is automatically closed (Gao Handover) to make room.

---

## Modules

The system is split into independent modules, wired together by the orchestrator.

### Core

| Module | Responsibility |
|--------|---------------|
| `capital_client` | Capital.com REST + WebSocket with auto-reconnect |
| `data_feed` | Candle buffers for 1min, 15min, and daily timeframes |
| `feature_engine` | Technical indicators (EMA, ATR, RSI, MACD, Bollinger, ADX, OBV, VWAP) and noise boundary computation |
| `statistics` | EV calculation, Kelly criterion, correlation matrix, adaptive parameters |
| `news_filter` | Economic calendar blackout periods |

### Pipeline

| Module | Responsibility |
|--------|---------------|
| `risk_constraints` | Pre-trade safety checks: session times, spread, margin, news, position limits |
| `setup_engine` | Noise Breakout detection and Gao Momentum signal |
| `trade_validator` | ATR-based SL computation, spread filter, EV gate (post-bootstrap) |
| `regime_classifier` | Market regime classification (BULLISH / BEARISH / BLOCKED / NEUTRAL) and volatility tagging |

### Execution

| Module | Responsibility |
|--------|---------------|
| `risk_manager` | Half-Kelly position sizing with correlation-adjusted exposure |
| `order_executor` | Order lifecycle (open, modify, close) |
| `trade_tracker` | Trade recording, feeds StatisticsEngine |
| `state_manager` | Daily P&L tracking, circuit breakers, kill switch |


---

## Orchestrator Pipeline

Every 1min candle triggers a multi-layer pipeline per instrument. Any layer can veto.

**Layer 1: Risk Constraints** check session times, spread, margin, news blackout, and position limits. The session close buffer (last 30 min) blocks NB but lets Gao through.

**Layer 2: Signal Detection** runs two paths:
- **NB:** At :00 and :30, checks if price has broken the noise boundary. Stops 60 min before close.
- **Gao:** 30 min before close, one-shot check if intraday return ≥ 0.1%. Bypasses BLOCKED regime and circuit breaker.

**Layer 3: Trade Validator** computes an ATR-based stop loss (1.5x ATR on 15min, adjusted for kurtosis and volatility). SL must be at least 3x the current spread. After 20 trades per strategy type, the EV gate blocks setups with negative expected value.

**Layer 4: Position Sizing** uses Half-Kelly from accumulated statistics, capped at 3% risk and 3x leverage. Correlation-adjusted exposure prevents hidden concentration across correlated instruments.

---

## Exit Management

**Stop Loss** is set at 1.5x ATR(15min), adjusted for kurtosis and volatility. Managed broker-side, executes even if the bot disconnects.

**Trailing Stop** activates when profit reaches 0.75R. Trail distance is the larger of 1.2x ATR or the stats-derived optimal distance. Lets winners run while locking in gains.

**Gao Handover** closes NB positions 30 min before session close to make room for Gao Momentum trades.

**EOD Close** fires 5 minutes before session end. All positions are closed. No overnight risk.

---

## Risk Constraints

| Check | Threshold | Applies to |
|-------|-----------|------------|
| Daily loss limit | 3% of balance from daily peak | Both |
| Position per instrument | Max 1 | Both |
| Simultaneous positions | 4 hard cap | Both |
| Correlated exposure | 2.5 effective positions | Both |
| Available margin | Min 20% of equity | Both |
| Spread filter | Blocked if spread > 1.5x average | Both |
| Session close buffer | No trades last 30min | NB only |
| BLOCKED regime | Extreme kurtosis or volatility | NB only |
| News blackout | 15 min before high-impact events | Both |
| Consecutive SL hits | 3 per instrument pauses it | NB only |
| Cooldowns | 60s global, 10min per signal/instrument | Both |

---

## Instruments

| Instrument | Epic | Session (UTC) | Gao Window (UTC) | EOD Close (UTC) |
|------------|------|---------------|-------------------|-----------------|
| DAX 40 | DE40 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |
| CAC 40 | FR40 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |
| NASDAQ 100 | US100 | 00:00 – 21:15 | 20:45 – 21:10 | 21:10 |
| S&P 500 | US500 | 00:00 – 21:15 | 20:45 – 21:10 | 21:10 |
| FTSE 100 | UK100 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |

Each instrument runs independently with its own signal state, position, and circuit breakers.

---

## Adaptive Statistics

All trading parameters adapt as trade data accumulates. NB and Gao have separate statistics.

| Parameter | Source | Fallback |
|-----------|--------|----------|
| Risk per trade | Half-Kelly fraction | 1.5% during bootstrap |
| Trailing distance | 30th percentile of win distribution | 1.2x ATR |
| EV gate | WR × avg_win − (1−WR) × avg_loss | Disabled until 20 trades |

Statistics are tracked per setup type and per instrument. Persisted in `stats.json`, trades in `trades.json`.

---

## References

- Zarattini, C., Aziz, A., & Barbon, A. (2024). *Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)*. Swiss Finance Institute. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172)
- Gao, L., Han, Y., Li, S. Z., & Zhou, G. (2018). *Market Intraday Momentum*. Journal of Financial Economics, 129(2), 394–414. [DOI](https://doi.org/10.1016/j.jfineco.2018.05.009)
- Maroy, A. (2025). *Improvements to Intraday Momentum Strategies Using Parameter Optimization and Different Exit Strategies*. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5095349)
- Independent replication on ES/NQ futures: [Quantitativo](https://www.quantitativo.com/p/intraday-momentum-for-es-and-nq)

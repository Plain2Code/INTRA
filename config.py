"""
Central configuration for the INTRA daytrading bot.

Infrastructure constants only: API URLs, credentials, instruments, sessions,
polling intervals, buffer sizes, and structural risk limits.

All trading parameters (SL/TP, signal thresholds, exit levels) are derived
from data by the StatisticsEngine – NO magic numbers here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time as dtime
from enum import Enum

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Capital.com credentials
# ---------------------------------------------------------------------------
CAPITAL_EMAIL: str = os.getenv("CAPITAL_EMAIL", "")
CAPITAL_PASSWORD: str = os.getenv("CAPITAL_PASSWORD", "")
CAPITAL_API_KEY: str = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_MODE: str = os.getenv("CAPITAL_MODE", "demo")  # "demo" or "live"

# ---------------------------------------------------------------------------
# Capital.com API URLs
# ---------------------------------------------------------------------------
API_URLS = {
    "demo": {
        "rest": "https://demo-api-capital.backend-capital.com/api/v1",
        "ws": "wss://demo-api-streaming-capital.backend-capital.com/connect",
    },
    "live": {
        "rest": "https://api-capital.backend-capital.com/api/v1",
        "ws": "wss://api-streaming-capital.backend-capital.com/connect",
    },
}

# Rate limiting
MAX_REQUESTS_PER_SECOND: int = 10
SESSION_TIMEOUT_SECONDS: int = 540  # refresh before 10min expiry

# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

class Instrument(Enum):
    DAX = "DE40"
    FR40 = "FR40"
    US100 = "US100"
    US500 = "US500"
    UK100 = "UK100"


@dataclass(frozen=True)
class SessionWindow:
    """Trading session in local exchange timezone (auto-adjusts for DST)."""
    open_time: dtime
    close_time: dtime
    timezone: str


INSTRUMENT_SESSIONS: dict[Instrument, SessionWindow] = {
    Instrument.DAX:    SessionWindow(dtime(0, 0),  dtime(21, 0),  "UTC"),
    Instrument.FR40:   SessionWindow(dtime(0, 0),  dtime(21, 0),  "UTC"),
    Instrument.US100:  SessionWindow(dtime(0, 0),  dtime(21, 15), "UTC"),
    Instrument.US500:  SessionWindow(dtime(0, 0),  dtime(21, 15), "UTC"),
    Instrument.UK100:  SessionWindow(dtime(0, 0),  dtime(21, 0),  "UTC"),
}

# Persistent files
ACTIVE_ASSETS_FILE: str = os.path.join(os.path.dirname(__file__), "active_assets.json")
TRADES_FILE: str = os.path.join(os.path.dirname(__file__), "trades.json")
STATS_FILE: str = os.path.join(os.path.dirname(__file__), "stats.json")

# ---------------------------------------------------------------------------
# Candle buffer sizes
# ---------------------------------------------------------------------------
BUFFER_1MIN: int = 500
BUFFER_15MIN: int = 200

# ---------------------------------------------------------------------------
# Indicator periods (standard, well-established values)
# ---------------------------------------------------------------------------
ADX_PERIOD: int = 14
EMA_FAST: int = 9
EMA_MID: int = 21
EMA_SLOW: int = 50
BB_PERIOD: int = 20
BB_STD: float = 2.0
ATR_PERIOD: int = 14
RSI_PERIOD: int = 14
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9
STOCH_K: int = 14
STOCH_SMOOTH: int = 3
VOLUME_MA_PERIOD: int = 20
VOLUME_DELTA_LOOKBACK: int = 20

# ---------------------------------------------------------------------------
# Statistical engine – bootstrap & adaptive parameters
# ---------------------------------------------------------------------------
BOOTSTRAP_MIN_TRADES: int = 20         # min trades before EV gate activates per setup type
KELLY_RAMP_START: int = 50             # min trades before Kelly sizing kicks in
KELLY_RAMP_END: int = 100             # trades at which full half-Kelly is used
PER_EPIC_MIN_TRADES: int = 5           # min trades before per-epic EV gate activates
MIN_CONFIDENCE: float = 0.0           # disabled – trade all breakouts, confidence is informational only
BOOTSTRAP_MIN_CONFIDENCE: float = 0.0  # disabled – trade all breakouts

# SL multiplier (ATR-based, no fixed TP — exits via trailing/EOD)
FALLBACK_SL_ATR_MULT: float = 1.5     # SL = 1.5 × ATR(15min)

# ---------------------------------------------------------------------------
# Noise Boundary Momentum (Zarattini, Aziz & Barbon 2024)
# ---------------------------------------------------------------------------
NOISE_LOOKBACK_DAYS: int = 14          # avg |daily_return| lookback
NOISE_ATR_FALLBACK_MULT: float = 0.6  # fallback boundary = ATR_15min * this (before 14 days data)
NOISE_CHECK_MINUTES: tuple[int, ...] = (0, 30)  # only check at :00 and :30
NOISE_MIN_BREAKOUT_ATR: float = 0.3   # min breakout beyond boundary in ATR units
BUFFER_DAILY: int = 30                 # daily candles to load (covers lookback + margin)

# Trailing stop (minimum floor, stats engine may set wider)
TRAILING_ACTIVATE_R: float = 0.75      # activate at +0.75R (no breakeven stage)
TRAILING_MIN_ATR_MULT: float = 1.2     # trail distance floor = 1.2 × ATR(1min) (must be < SL=1.5 ATR)

# Hard SL sanity filter
MIN_SL_SPREAD_MULT: float = 3.0       # SL must be >= 3× spread

# ---------------------------------------------------------------------------
# Correlation matrix (empirical intraday, updated periodically)
# Symmetric matrix: CORRELATION[a][b] = CORRELATION[b][a]
# ---------------------------------------------------------------------------
CORRELATION_MATRIX: dict[str, dict[str, float]] = {
    "DE40":  {"DE40": 1.0, "FR40": 0.92, "US100": 0.82, "US500": 0.85, "UK100": 0.88},
    "FR40":  {"DE40": 0.92, "FR40": 1.0, "US100": 0.80, "US500": 0.83, "UK100": 0.90},
    "US100": {"DE40": 0.82, "FR40": 0.80, "US100": 1.0, "US500": 0.95, "UK100": 0.78},
    "US500": {"DE40": 0.85, "FR40": 0.83, "US100": 0.95, "US500": 1.0, "UK100": 0.80},
    "UK100": {"DE40": 0.88, "FR40": 0.90, "US100": 0.78, "US500": 0.80, "UK100": 1.0},
}

# ---------------------------------------------------------------------------
# Risk constraints (structural limits, not heuristics)
# ---------------------------------------------------------------------------
MAX_DAILY_LOSS_PCT: float = 0.03       # 3% daily max drawdown
MAX_CONSECUTIVE_SL_PER_EPIC: int = 3   # circuit breaker per instrument
SPREAD_THRESHOLD_MULT: float = 1.5     # block if spread > 1.5× average
RISK_PER_TRADE_PCT: float = 0.015      # base risk (overridden by Kelly when data available) -> use 1% for Balance over 2k
MAX_KELLY_RISK_PCT: float = 0.03       # cap even if Kelly says more
MIN_BOOTSTRAP_RISK_PCT: float = 0.005  # minimal risk during bootstrap / negative edge
MAX_TRADE_LEVERAGE: float = 3.0        # max notional = balance × this
INSTRUMENT_MAX_LEVERAGE: dict[str, float] = {}
MIN_AVAILABLE_PCT: float = 0.20        # block if margin too low
MIN_EFFECTIVE_RISK_RATIO: float = 0.10 # skip if leverage cap reduces risk too much
MAX_CORRELATED_EXPOSURE: float = 2.5   # max effective (correlation-adjusted) positions
MAX_SIMULTANEOUS_POSITIONS: int = 4  # hard cap (correlation limit does real work)
ORDER_REJECT_MAX: int = 3
ORDER_REJECT_PAUSE_MINUTES: int = 60

# Capital.com minimum deal sizes
INSTRUMENT_MIN_SIZE: dict[str, float] = {
    "DE40":   0.10,
    "FR40":   0.10,
    "US100":  0.10,
    "US500":  0.10,
    "UK100":  0.10,
}

# Session window protection
SESSION_NO_NEW_TRADE_OPEN_BUFFER: int = 30
SESSION_NO_NEW_TRADE_BUFFER: int = 30
SESSION_FORCE_CLOSE_BUFFER: int = 5

# Signal cooldowns
SIGNAL_COOLDOWN_MINUTES: int = 10
GLOBAL_COOLDOWN_SECONDS: int = 60

# News blackout
NEWS_BLACKOUT_BEFORE_MINUTES: int = 15
NEWS_BLACKOUT_AFTER_MINUTES: int = 0
NEWS_REFRESH_HOURS: float = 6.0

# ---------------------------------------------------------------------------
# Polling intervals (seconds)
# ---------------------------------------------------------------------------
POLL_BALANCE_INTERVAL: float = 10.0
POLL_POSITIONS_INTERVAL: float = 2.0

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = 8080

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

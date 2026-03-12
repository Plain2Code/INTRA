"""
Feature engine – all technical indicator computations.

Standalone module: maintains two IndicatorSuite instances per instrument
(1min and 15min).  Uses talipp for O(1) incremental updates plus custom
implementations for Volume Delta, VWAP daily reset, and candlestick
pattern detection.

Every public method is independently callable with any OHLCVCandle data.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from talipp.indicators import ADX, EMA, BB, ATR, RSI, MACD, Stoch, VWAP, OBV
from talipp.ohlcv import OHLCV

from core.data_feed import OHLCVCandle
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candlestick patterns
# ---------------------------------------------------------------------------

class PatternType(Enum):
    HAMMER = "hammer"
    SHOOTING_STAR = "shooting_star"
    DOJI = "doji"
    BULLISH_ENGULFING = "bullish_engulfing"
    BEARISH_ENGULFING = "bearish_engulfing"
    STRONG_BULL = "strong_bull"
    STRONG_BEAR = "strong_bear"


@dataclass
class CandlePattern:
    pattern: PatternType
    strength: float  # 0.0 to 1.0


class CandlestickDetector:
    """Detect candlestick patterns.  Tuned for 1min candles."""

    def __init__(
        self,
        doji_body_pct: float = 0.05,
        hammer_wick_ratio: float = 2.0,
        strong_body_pct: float = 0.70,
        engulf_min_body_pct: float = 0.03,
    ):
        self._doji_pct = doji_body_pct
        self._hammer_ratio = hammer_wick_ratio
        self._strong_pct = strong_body_pct
        self._engulf_min = engulf_min_body_pct
        self._prev: OHLCVCandle | None = None

    def detect(self, candle: OHLCVCandle) -> list[CandlePattern]:
        patterns: list[CandlePattern] = []
        o, h, l, c = candle.open, candle.high, candle.low, candle.close
        full_range = h - l

        if full_range == 0:
            self._prev = candle
            return patterns

        body = abs(c - o)
        body_pct = body / full_range
        is_bull = c >= o
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # Doji
        if body_pct <= self._doji_pct:
            patterns.append(CandlePattern(
                PatternType.DOJI, 1.0 - (body_pct / self._doji_pct)
            ))

        if body > 0 and body_pct > self._doji_pct:
            # Hammer (bullish reversal signal)
            if lower_wick >= self._hammer_ratio * body and upper_wick <= body * 0.5:
                strength = min(1.0, lower_wick / (self._hammer_ratio * body * 2))
                patterns.append(CandlePattern(PatternType.HAMMER, strength))

            # Shooting star (bearish reversal signal)
            if upper_wick >= self._hammer_ratio * body and lower_wick <= body * 0.5:
                strength = min(1.0, upper_wick / (self._hammer_ratio * body * 2))
                patterns.append(CandlePattern(PatternType.SHOOTING_STAR, strength))

        # Strong bull / strong bear
        if body_pct >= self._strong_pct:
            if is_bull:
                patterns.append(CandlePattern(PatternType.STRONG_BULL, body_pct))
            else:
                patterns.append(CandlePattern(PatternType.STRONG_BEAR, body_pct))

        # Engulfing (needs previous candle)
        if self._prev is not None:
            po, pc = self._prev.open, self._prev.close
            prev_range = self._prev.high - self._prev.low
            if prev_range > 0:
                prev_body = abs(pc - po)
                prev_body_pct = prev_body / prev_range
                prev_bull = pc >= po

                if body_pct > self._engulf_min and prev_body_pct > self._engulf_min:
                    # Bullish engulfing
                    if not prev_bull and is_bull and c > po and o < pc:
                        strength = min(1.0, body / (prev_body + 1e-9))
                        patterns.append(CandlePattern(PatternType.BULLISH_ENGULFING, strength))

                    # Bearish engulfing
                    if prev_bull and not is_bull and c < po and o > pc:
                        strength = min(1.0, body / (prev_body + 1e-9))
                        patterns.append(CandlePattern(PatternType.BEARISH_ENGULFING, strength))

        self._prev = candle
        return patterns


# ---------------------------------------------------------------------------
# Volume Delta
# ---------------------------------------------------------------------------

class VolumeDelta:
    """
    Approximate buying/selling pressure from candle data.
    buy_ratio  = (close - low) / (high - low)
    sell_ratio = (high - close) / (high - low)
    delta      = volume * (buy_ratio - sell_ratio)
    """

    def __init__(self, lookback: int = 20):
        self._lookback = lookback
        self._deltas: deque[float] = deque(maxlen=lookback)

    def add(self, candle: OHLCVCandle) -> float:
        hl = candle.high - candle.low
        if hl == 0:
            self._deltas.append(0.0)
            return 0.0
        buy_ratio = (candle.close - candle.low) / hl
        sell_ratio = (candle.high - candle.close) / hl
        delta = candle.volume * (buy_ratio - sell_ratio)
        self._deltas.append(delta)
        return delta

    @property
    def current(self) -> float:
        return self._deltas[-1] if self._deltas else 0.0

    @property
    def cumulative(self) -> float:
        return sum(self._deltas)


# ---------------------------------------------------------------------------
# VWAP with daily reset
# ---------------------------------------------------------------------------

class VWAPDaily:
    """VWAP that resets at the start of each trading day."""

    def __init__(self):
        self._vwap_indicator: VWAP | None = None
        self._current_date: date | None = None

    def add(self, candle: OHLCVCandle):
        ohlcv = _to_talipp(candle)
        candle_date = candle.timestamp.date()

        if self._current_date is None or candle_date != self._current_date:
            self._current_date = candle_date
            self._vwap_indicator = VWAP(input_values=[ohlcv])
        else:
            self._vwap_indicator.add(ohlcv)

    @property
    def value(self) -> float | None:
        if self._vwap_indicator and len(self._vwap_indicator) > 0:
            v = self._vwap_indicator[-1]
            return v if v is not None else None
        return None


# ---------------------------------------------------------------------------
# Volume MA Ratio
# ---------------------------------------------------------------------------

class VolumeMA:
    """Current volume relative to its simple moving average."""

    def __init__(self, period: int = 20):
        self._period = period
        self._volumes: deque[float] = deque(maxlen=period)

    def add(self, volume: float):
        self._volumes.append(volume)

    @property
    def ratio(self) -> float | None:
        if len(self._volumes) < self._period:
            return None
        avg = sum(self._volumes) / len(self._volumes)
        if avg == 0:
            return None
        return self._volumes[-1] / avg


# ---------------------------------------------------------------------------
# Divergence Detector (for 15min regime classification)
# ---------------------------------------------------------------------------

class DivergenceDetector:
    """
    Detect price/indicator divergence by comparing swing extremes
    across two halves of a rolling window.

    Bullish divergence: price makes lower low, but RSI/MACD makes higher low.
    Bearish divergence: price makes higher high, but RSI/MACD makes lower high.
    """

    def __init__(self, window: int = 20):
        self._window = window
        # Each entry: (close, rsi, macd_hist)
        self._data: deque[tuple[float, float | None, float | None]] = deque(maxlen=window)

    def add(self, close: float, rsi: float | None, macd_hist: float | None):
        self._data.append((close, rsi, macd_hist))

    def detect(self) -> dict[str, bool]:
        """Return divergence flags."""
        result = {
            "rsi_bullish_div": False,
            "rsi_bearish_div": False,
            "macd_bullish_div": False,
            "macd_bearish_div": False,
        }

        if len(self._data) < 10:
            return result

        data = list(self._data)
        n = len(data)
        mid = n // 2
        first_half = data[:mid]
        second_half = data[mid:]

        # --- Bullish divergence: price lower low, indicator higher low ---
        min1_idx = min(range(len(first_half)), key=lambda i: first_half[i][0])
        min2_idx = min(range(len(second_half)), key=lambda i: second_half[i][0])

        p1, rsi1_low, macd1_low = first_half[min1_idx]
        p2, rsi2_low, macd2_low = second_half[min2_idx]

        if p2 < p1:  # Price made lower low
            if rsi1_low is not None and rsi2_low is not None and rsi2_low > rsi1_low:
                result["rsi_bullish_div"] = True
            if macd1_low is not None and macd2_low is not None and macd2_low > macd1_low:
                result["macd_bullish_div"] = True

        # --- Bearish divergence: price higher high, indicator lower high ---
        max1_idx = max(range(len(first_half)), key=lambda i: first_half[i][0])
        max2_idx = max(range(len(second_half)), key=lambda i: second_half[i][0])

        p1, rsi1_high, macd1_high = first_half[max1_idx]
        p2, rsi2_high, macd2_high = second_half[max2_idx]

        if p2 > p1:  # Price made higher high
            if rsi1_high is not None and rsi2_high is not None and rsi2_high < rsi1_high:
                result["rsi_bearish_div"] = True
            if macd1_high is not None and macd2_high is not None and macd2_high < macd1_high:
                result["macd_bearish_div"] = True

        return result


# ---------------------------------------------------------------------------
# BB Width
# ---------------------------------------------------------------------------

def bb_width(bb_val) -> float | None:
    """Calculate Bollinger Band width as (upper - lower) / middle."""
    if bb_val is None:
        return None
    try:
        if bb_val.cb and bb_val.cb != 0:
            return (bb_val.ub - bb_val.lb) / bb_val.cb
    except (AttributeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Helper: convert OHLCVCandle to talipp OHLCV
# ---------------------------------------------------------------------------

def _to_talipp(candle: OHLCVCandle) -> OHLCV:
    return OHLCV(
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        time=candle.timestamp,
    )


def _safe_get(indicator, idx: int = -1):
    """Safely extract the last value from a talipp indicator."""
    try:
        v = indicator[idx]
        return v if v is not None else None
    except (IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Indicator Suite
# ---------------------------------------------------------------------------

class IndicatorSuite:
    """
    Complete set of indicators for one instrument + timeframe.

    Initialize with historical candles, then call .add(candle) for each
    new closed candle.  Call .snapshot() to get all current values.
    """

    def __init__(self, candles: list[OHLCVCandle], is_15min: bool = False):
        ohlcv_list = [_to_talipp(c) for c in candles]
        close_list = [c.close for c in candles]

        self.is_15min = is_15min

        # -- Trend --
        self.adx = ADX(config.ADX_PERIOD, config.ADX_PERIOD, input_values=ohlcv_list)
        self.ema_9 = EMA(config.EMA_FAST, input_values=close_list)
        self.ema_21 = EMA(config.EMA_MID, input_values=close_list)
        self.ema_50 = EMA(config.EMA_SLOW, input_values=close_list)

        # -- Volatility --
        self.bb = BB(config.BB_PERIOD, config.BB_STD, input_values=close_list)
        self.atr = ATR(config.ATR_PERIOD, input_values=ohlcv_list)

        # -- Momentum --
        self.rsi = RSI(config.RSI_PERIOD, input_values=close_list)
        self.macd = MACD(config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL,
                         input_values=close_list)
        self.stoch = Stoch(config.STOCH_K, config.STOCH_SMOOTH, input_values=ohlcv_list)

        # -- Volume --
        self.obv = OBV(input_values=ohlcv_list)
        self.volume_delta = VolumeDelta(config.VOLUME_DELTA_LOOKBACK)
        self.volume_ma = VolumeMA(config.VOLUME_MA_PERIOD)
        self.vwap = VWAPDaily()

        # -- Patterns (1min only) --
        self.pattern_detector = CandlestickDetector() if not is_15min else None

        # Feed historical candles through custom indicators
        for c in candles:
            self.volume_delta.add(c)
            self.volume_ma.add(c.volume)
            self.vwap.add(c)
            if self.pattern_detector:
                self.pattern_detector.detect(c)

        # Microstructure: close history for returns and body/range
        self._last_body_range: float = 0.0
        self._close_history: deque[float] = deque(maxlen=50)
        self._return_history: deque[float] = deque(maxlen=50)
        self._vwap_deviation_history: deque[float] = deque(maxlen=10)
        self._last_candle_spread: float = 0.0
        for c in candles:
            fr = c.high - c.low
            if fr > 0:
                self._last_body_range = abs(c.close - c.open) / fr
            self._close_history.append(c.close)
            if len(self._close_history) >= 2:
                prev = self._close_history[-2]
                if prev != 0:
                    self._return_history.append((c.close - prev) / prev)
            self._last_candle_spread = getattr(c, 'spread', 0.0)

        # RSI percentile tracking (rolling window for adaptive thresholds)
        self._rsi_history: deque[float] = deque(maxlen=100)
        n_rsi = len(self.rsi)
        for i in range(max(0, n_rsi - 100), n_rsi):
            rv = self.rsi[i]
            if rv is not None:
                self._rsi_history.append(float(rv))

        # ADX median tracking (rolling window for adaptive threshold)
        self._adx_recent: deque[float] = deque(maxlen=20)

        # ADX history for falling detection (15min only)
        self._adx_history: deque[float] = deque(maxlen=10)
        n_adx = len(self.adx)
        for i in range(max(0, n_adx - 10), n_adx):
            av = self.adx[i]
            if av is not None:
                try:
                    v = av.adx if hasattr(av, 'adx') else float(av)
                    self._adx_history.append(v)
                except (TypeError, ValueError):
                    pass

        # BB Width history for comparison
        self._bb_width_history: deque[float] = deque(maxlen=20)
        n_bb = len(self.bb)
        for i in range(max(0, n_bb - 20), n_bb):
            bw_val = bb_width(self.bb[i])
            if bw_val is not None:
                self._bb_width_history.append(bw_val)

        # ATR history for rolling average (volatility classification)
        self._atr_history: deque[float] = deque(maxlen=20)
        n_atr = len(self.atr)
        for i in range(max(0, n_atr - 20), n_atr):
            atr_val = self.atr[i]
            if atr_val is not None:
                try:
                    self._atr_history.append(float(atr_val))
                except (TypeError, ValueError):
                    pass

        # MACD hist history – used by REV (direction check) and TC (hist > 0)
        self._macd_hist_history: deque[float] = deque(maxlen=5)
        n_macd = len(self.macd)
        for i in range(max(0, n_macd - 5), n_macd):
            mv = self.macd[i]
            if mv and mv.macd is not None and mv.signal is not None:
                self._macd_hist_history.append(mv.macd - mv.signal)

        # Divergence detector (15min only) – populated from historical data
        self.divergence = DivergenceDetector(window=20) if is_15min else None
        if self.divergence:
            n_ind = len(self.rsi)
            start = max(0, n_ind - 20)
            for i in range(start, n_ind):
                rsi_val = self.rsi[i] if self.rsi[i] is not None else None
                macd_val = self.macd[i]
                macd_h = None
                if macd_val and macd_val.macd is not None and macd_val.signal is not None:
                    macd_h = macd_val.macd - macd_val.signal
                self.divergence.add(candles[i].close, rsi_val, macd_h)

        self._candle_count = len(candles)

    def add(self, candle: OHLCVCandle):
        """Add a new closed candle to all indicators.  O(1) per indicator."""
        ohlcv = _to_talipp(candle)
        close = candle.close

        # talipp indicators
        self.adx.add(ohlcv)
        self.atr.add(ohlcv)
        self.stoch.add(ohlcv)
        self.obv.add(ohlcv)
        self.ema_9.add(close)
        self.ema_21.add(close)
        self.ema_50.add(close)
        self.bb.add(close)
        self.rsi.add(close)
        self.macd.add(close)

        # Custom indicators
        self.volume_delta.add(candle)
        self.volume_ma.add(candle.volume)
        self.vwap.add(candle)

        # Track body/range ratio
        full_range = candle.high - candle.low
        if full_range > 0:
            self._last_body_range = abs(candle.close - candle.open) / full_range
        else:
            self._last_body_range = 0.0

        # Track close history and returns (microstructure)
        self._close_history.append(candle.close)
        if len(self._close_history) >= 2:
            prev = self._close_history[-2]
            if prev != 0:
                self._return_history.append((candle.close - prev) / prev)
        self._last_candle_spread = getattr(candle, 'spread', 0.0)

        # Track RSI history for percentile computation
        rsi_val = _safe_get(self.rsi)
        if rsi_val is not None:
            try:
                self._rsi_history.append(float(rsi_val))
            except (TypeError, ValueError):
                pass

        # Track ADX history
        adx_val = _safe_get(self.adx)
        if adx_val is not None:
            try:
                v = adx_val.adx if hasattr(adx_val, 'adx') else float(adx_val)
                self._adx_history.append(v)
            except (TypeError, ValueError):
                pass
            try:
                v = adx_val.adx if hasattr(adx_val, 'adx') else float(adx_val)
                self._adx_recent.append(v)
            except (TypeError, ValueError):
                pass

        # Track ATR history
        atr_val_raw = _safe_get(self.atr)
        if atr_val_raw is not None:
            try:
                self._atr_history.append(float(atr_val_raw))
            except (TypeError, ValueError):
                pass

        # Track BB Width history
        bw = bb_width(_safe_get(self.bb))
        if bw is not None:
            self._bb_width_history.append(bw)

        # Track MACD hist history (both timeframes – used for direction check)
        _mv = _safe_get(self.macd)
        if _mv and _mv.macd is not None and _mv.signal is not None:
            self._macd_hist_history.append(_mv.macd - _mv.signal)

        # Feed divergence detector (15min only)
        if self.divergence:
            rsi_val = _safe_get(self.rsi)
            macd_val = _safe_get(self.macd)
            macd_h = None
            if macd_val and macd_val.macd is not None and macd_val.signal is not None:
                macd_h = macd_val.macd - macd_val.signal
            self.divergence.add(candle.close, rsi_val, macd_h)

        self._candle_count += 1

        # Memory management: purge old values every 100 candles
        if self._candle_count % 100 == 0:
            self._purge()

    # --- Microstructure helper methods ---

    def _body_range_ratio(self) -> float:
        """Ratio of last candle body to full range. High = conviction."""
        return self._last_body_range

    def _return_n(self, n: int) -> float:
        """N-candle return."""
        if len(self._close_history) <= n:
            return 0.0
        prev = self._close_history[-(n + 1)]
        cur = self._close_history[-1]
        if prev == 0:
            return 0.0
        return (cur - prev) / prev

    def _return_kurtosis(self) -> float:
        """Excess kurtosis of 1-minute returns. >3 = fat tails."""
        if len(self._return_history) < 20:
            return 3.0  # normal distribution default
        rets = list(self._return_history)
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / n
        if var == 0:
            return 3.0
        m4 = sum((r - mean) ** 4 for r in rets) / n
        return m4 / (var ** 2)

    def _obv_slope(self) -> float:
        """Slope of OBV over last 5 candles (normalized by volume)."""
        n_obv = len(self.obv)
        if n_obv < 6:
            return 0.0
        try:
            obv_now = self.obv[-1]
            obv_5 = self.obv[-6]
            if obv_now is None or obv_5 is None:
                return 0.0
            return (float(obv_now) - float(obv_5)) / 5
        except (TypeError, IndexError):
            return 0.0

    def _rsi_percentile(self) -> float:
        """Current RSI as percentile of its recent 100-candle distribution."""
        rsi_val = _safe_get(self.rsi)
        if rsi_val is None or len(self._rsi_history) < 20:
            return 0.5
        try:
            current = float(rsi_val)
        except (TypeError, ValueError):
            return 0.5
        below = sum(1 for r in self._rsi_history if r <= current)
        return below / len(self._rsi_history)

    def _adx_median(self) -> float:
        """Median ADX over last 20 candles."""
        if len(self._adx_recent) < 5:
            return 20.0  # neutral default
        sorted_adx = sorted(self._adx_recent)
        return sorted_adx[len(sorted_adx) // 2]

    def _vwap_deviation_decreasing(self, close: float, vwap: float | None, atr: float | None) -> bool:
        """Track if VWAP deviation is decreasing (price reverting)."""
        if vwap is None or atr is None or atr == 0:
            return False
        dev = abs(close - vwap) / atr
        self._vwap_deviation_history.append(dev)
        if len(self._vwap_deviation_history) < 3:
            return False
        # Decreasing if last 2 deviations are lower than the one before
        return (self._vwap_deviation_history[-1] < self._vwap_deviation_history[-2]
                and self._vwap_deviation_history[-2] < self._vwap_deviation_history[-3])

    def _purge(self):
        max_keep = 600
        for ind in (self.adx, self.ema_9, self.ema_21, self.ema_50,
                    self.bb, self.atr, self.rsi, self.macd, self.stoch, self.obv):
            if len(ind) > max_keep:
                ind.purge_oldest(len(ind) - max_keep)

    def snapshot(self) -> dict[str, Any]:
        """Return all current indicator values as a flat dict."""
        adx_val = _safe_get(self.adx)
        bb_val = _safe_get(self.bb)
        macd_val = _safe_get(self.macd)
        stoch_val = _safe_get(self.stoch)

        # Extract ADX components
        adx_value = None
        plus_di = None
        minus_di = None
        if adx_val is not None:
            try:
                adx_value = adx_val.adx if hasattr(adx_val, 'adx') else float(adx_val)
                plus_di = adx_val.plus_di if hasattr(adx_val, 'plus_di') else None
                minus_di = adx_val.minus_di if hasattr(adx_val, 'minus_di') else None
            except (TypeError, ValueError):
                pass

        # BB components
        bb_upper = bb_val.ub if bb_val else None
        bb_middle = bb_val.cb if bb_val else None
        bb_lower = bb_val.lb if bb_val else None
        bw = bb_width(bb_val)

        # BB width moving average
        bb_width_ma = None
        if len(self._bb_width_history) >= 20:
            bb_width_ma = sum(self._bb_width_history) / len(self._bb_width_history)

        # MACD components
        macd_line = macd_val.macd if macd_val else None
        macd_signal = macd_val.signal if macd_val else None
        macd_hist = None
        if macd_val and macd_val.macd is not None and macd_val.signal is not None:
            macd_hist = macd_val.macd - macd_val.signal

        # Previous MACD hist (for direction-of-change checks in REV / TC)
        macd_hist_prev = (
            self._macd_hist_history[-2]
            if len(self._macd_hist_history) >= 2
            else None
        )

        # Stoch components
        stoch_k = stoch_val.k if stoch_val else None
        stoch_d = stoch_val.d if stoch_val else None

        # ADX falling detection (for REVERSING)
        adx_falling_consecutive = 0
        if len(self._adx_history) >= 2:
            for i in range(len(self._adx_history) - 1, 0, -1):
                if self._adx_history[i] < self._adx_history[i - 1]:
                    adx_falling_consecutive += 1
                else:
                    break

        # ADX rate of change (for ACCELERATING)
        adx_roc = None
        if len(self._adx_history) >= 4:
            adx_roc = (self._adx_history[-1] - self._adx_history[-4]) / 3

        # Raw ATR for microstructure calculations
        atr_raw = _safe_get(self.atr)
        try:
            atr_raw = float(atr_raw) if atr_raw is not None else None
        except (TypeError, ValueError):
            atr_raw = None

        # Divergence flags (15min only)
        div = self.divergence.detect() if self.divergence else {}

        return {
            "ready": self._candle_count >= 50,

            # Trend
            "adx": adx_value,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "adx_falling_consecutive": adx_falling_consecutive,
            "adx_roc": adx_roc,
            "ema_9": _safe_get(self.ema_9),
            "ema_21": _safe_get(self.ema_21),
            "ema_50": _safe_get(self.ema_50),

            # Volatility
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "bb_width": bw,
            "bb_width_ma": bb_width_ma,
            "atr": _safe_get(self.atr),
            "atr_avg": (sum(self._atr_history) / len(self._atr_history)
                        if len(self._atr_history) >= 10 else None),

            # Momentum
            "rsi": _safe_get(self.rsi),
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "macd_hist_prev": macd_hist_prev,
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,

            # Volume
            "vwap": self.vwap.value,
            "obv": _safe_get(self.obv),
            "volume_delta": self.volume_delta.current,
            "volume_delta_cumulative": self.volume_delta.cumulative,
            "volume_ma_ratio": self.volume_ma.ratio,

            # Divergence (15min only, False for 1min)
            "rsi_bullish_div": div.get("rsi_bullish_div", False),
            "rsi_bearish_div": div.get("rsi_bearish_div", False),
            "macd_bullish_div": div.get("macd_bullish_div", False),
            "macd_bearish_div": div.get("macd_bearish_div", False),

            # Microstructure features
            "volume_delta_ratio": (self.volume_delta.current / atr_raw
                                   if atr_raw and atr_raw > 0 else 0.0),
            "spread_atr_ratio": (self._last_candle_spread / atr_raw
                                 if atr_raw and atr_raw > 0 else 0.0),
            "body_range_ratio": self._body_range_ratio(),
            "return_5": self._return_n(5),
            "return_kurtosis": self._return_kurtosis(),
            "obv_slope": self._obv_slope(),

            # Adaptive thresholds (percentile-based)
            "rsi_percentile": self._rsi_percentile(),
            "adx_median": self._adx_median(),

            # VWAP deviation tracking
            "vwap_deviation_decreasing": self._vwap_deviation_decreasing(
                _safe_get(self.ema_9) or 0, self.vwap.value, atr_raw
            ),
        }


# ---------------------------------------------------------------------------
# Feature Engine (per instrument, both timeframes)
# ---------------------------------------------------------------------------

class FeatureEngine:
    """
    Manages indicator suites for one instrument across both timeframes.

    Usage:
        engine = FeatureEngine("US100")
        engine.initialize(candles_1min, candles_15min, candles_daily)
        engine.update_1min(new_candle)
        snap_1min = engine.get_1min_snapshot()
        snap_15min = engine.get_15min_snapshot()
    """

    def __init__(self, epic: str):
        self.epic = epic
        self._suite_1min: IndicatorSuite | None = None
        self._suite_15min: IndicatorSuite | None = None
        self._pattern_detector = CandlestickDetector()

        # Noise Boundary Momentum: daily data
        self._daily_opens: deque[float] = deque(maxlen=config.BUFFER_DAILY)
        self._daily_returns: deque[float] = deque(maxlen=config.BUFFER_DAILY)
        self._current_daily_open: float | None = None
        self._current_daily_date: date | None = None

    def initialize(
        self,
        candles_1min: list[OHLCVCandle],
        candles_15min: list[OHLCVCandle],
        candles_daily: list[OHLCVCandle] | None = None,
    ):
        """Initialize indicator suites with historical data."""
        if candles_1min:
            self._suite_1min = IndicatorSuite(candles_1min, is_15min=False)

        if candles_15min:
            self._suite_15min = IndicatorSuite(candles_15min, is_15min=True)

        # Build daily returns from historical daily candles
        if candles_daily:
            for c in candles_daily:
                self._daily_opens.append(c.open)
                if c.open > 0:
                    daily_ret = (c.close - c.open) / c.open
                    self._daily_returns.append(abs(daily_ret))
            # Set current daily open from last daily candle's open
            if candles_daily:
                last_daily = candles_daily[-1]
                self._current_daily_open = last_daily.open
                self._current_daily_date = last_daily.timestamp.date()
            logger.debug("Daily data for %s: %d returns, open=%.2f",
                         self.epic, len(self._daily_returns), self._current_daily_open or 0)

    def update_1min(self, candle: OHLCVCandle):
        # Track daily open: reset on new trading day
        candle_date = candle.timestamp.date()
        if self._current_daily_date is None or candle_date != self._current_daily_date:
            # New day: record yesterday's return if we have an open
            if self._current_daily_open is not None and self._suite_1min:
                prev_close = candle.open  # last candle's close ≈ new candle's open
                if self._current_daily_open > 0:
                    daily_ret = abs((prev_close - self._current_daily_open) / self._current_daily_open)
                    self._daily_returns.append(daily_ret)
            self._current_daily_open = candle.open
            self._current_daily_date = candle_date
            self._daily_opens.append(candle.open)
            logger.debug("[%s] New trading day: daily_open=%.2f", self.epic, candle.open)

        if self._suite_1min:
            self._suite_1min.add(candle)

    def update_15min(self, candle: OHLCVCandle):
        if self._suite_15min:
            self._suite_15min.add(candle)

    def get_1min_snapshot(self) -> dict[str, Any]:
        if self._suite_1min is None:
            return {"ready": False}
        snap = self._suite_1min.snapshot()

        # Add noise boundary data
        snap["daily_open"] = self._current_daily_open
        snap["noise_boundary_width"] = self._get_noise_boundary_width()
        if self._current_daily_open is not None and snap["noise_boundary_width"] is not None:
            snap["noise_upper"] = self._current_daily_open + snap["noise_boundary_width"]
            snap["noise_lower"] = self._current_daily_open - snap["noise_boundary_width"]
        else:
            snap["noise_upper"] = None
            snap["noise_lower"] = None

        return snap

    def get_15min_snapshot(self) -> dict[str, Any]:
        if self._suite_15min is None:
            return {"ready": False}
        return self._suite_15min.snapshot()

    def _get_noise_boundary_width(self) -> float | None:
        """
        Noise boundary width = avg(|daily_return|, NOISE_LOOKBACK_DAYS) * daily_open.

        Falls back to ATR_15min * NOISE_ATR_FALLBACK_MULT if insufficient daily data.
        """
        if self._current_daily_open is None or self._current_daily_open == 0:
            return None

        if len(self._daily_returns) >= config.NOISE_LOOKBACK_DAYS:
            # Use last N days of absolute daily returns
            recent = list(self._daily_returns)[-config.NOISE_LOOKBACK_DAYS:]
            avg_abs_return = sum(recent) / len(recent)
            return avg_abs_return * self._current_daily_open

        # Fallback: use 15min ATR as proxy
        atr_15 = self.atr_15min
        if atr_15 is not None and atr_15 > 0:
            return atr_15 * config.NOISE_ATR_FALLBACK_MULT

        return None

    def detect_patterns(self, candle: OHLCVCandle) -> list[CandlePattern]:
        """Detect candlestick patterns on 1min candle."""
        return self._pattern_detector.detect(candle)

    @property
    def atr_1min(self) -> float | None:
        if self._suite_1min:
            return _safe_get(self._suite_1min.atr)
        return None

    @property
    def atr_15min(self) -> float | None:
        if self._suite_15min:
            return _safe_get(self._suite_15min.atr)
        return None

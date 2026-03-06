"""
Market Context – Schicht 1.

Classifies 15min market bias and volatility for directional context.
Returns MarketBias (BULLISH, BEARISH, NEUTRAL, BLOCKED) and a volatility
tag, plus microstructure context (volume pressure, trend quality).

Classification:
  1. BLOCKED     – extreme acceleration or fat tails (kurtosis)
  2. BULLISH     – EMA9 > EMA21 AND ADX above its rolling median
  3. BEARISH     – EMA9 < EMA21 AND ADX above its rolling median
  4. NEUTRAL     – everything else (ambiguous = tradeable, not blocked)

All thresholds are data-adaptive (ADX median, ATR ratio) – no magic numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MarketBias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    BLOCKED = "BLOCKED"


@dataclass
class BiasResult:
    bias: MarketBias
    adx: float
    atr_15min: float
    volatility: str   # "low" | "normal" | "high" | "extreme"
    details: str
    volume_pressure: str = "neutral"  # "buying" | "selling" | "neutral"
    trend_quality: float = 0.0        # 0-1, how clean is the trend
    blocked_reason: str = ""          # "kurtosis" | "adx_acceleration" | "" (empty if not blocked)


def classify_bias(snapshot: dict[str, Any]) -> BiasResult:
    """
    Classify current market bias from a 15min feature snapshot.

    Uses data-adaptive thresholds:
    - ADX vs its own rolling median (not hardcoded)
    - EMA convergence relative to ATR
    - Volume delta for pressure detection
    - OBV slope for trend confirmation
    """
    if not snapshot.get("ready", False):
        return BiasResult(MarketBias.NEUTRAL, 0.0, 0.0, "normal", "Indicators not ready")

    adx = snapshot.get("adx")
    ema_9 = snapshot.get("ema_9")
    ema_21 = snapshot.get("ema_21")
    atr = snapshot.get("atr")
    adx_roc = snapshot.get("adx_roc")

    if any(v is None for v in (adx, ema_9, ema_21, atr)):
        return BiasResult(MarketBias.NEUTRAL, 0.0, 0.0, "normal", "Insufficient indicator data")

    if atr == 0:
        return BiasResult(MarketBias.NEUTRAL, adx, 0.0, "normal", "ATR is zero")

    # Volatility tag from ATR ratio
    atr_avg = snapshot.get("atr_avg")
    volatility = _classify_volatility(atr, atr_avg)

    # Microstructure: volume pressure and OBV
    volume_pressure = _classify_volume_pressure(snapshot)
    obv_slope = snapshot.get("obv_slope", 0.0) or 0.0

    # ------------------------------------------------------------------
    # 1. BLOCKED – extreme conditions
    # ------------------------------------------------------------------
    blocked = _check_blocked(adx, adx_roc, snapshot)
    if blocked is not None:
        blocked.atr_15min = atr
        blocked.volatility = volatility
        blocked.volume_pressure = volume_pressure
        return blocked

    # ------------------------------------------------------------------
    # Adaptive ADX threshold: use rolling median instead of hardcoded
    # ------------------------------------------------------------------
    adx_median = snapshot.get("adx_median")
    adx_threshold = adx_median if adx_median and adx_median > 10 else 18.0

    # ------------------------------------------------------------------
    # 2. BULLISH / BEARISH – directional bias
    # ------------------------------------------------------------------
    if adx > adx_threshold:
        # EMA convergence check: spread must be meaningful relative to ATR
        ema_spread = abs(ema_9 - ema_21)
        if ema_spread < 0.15 * atr:
            trend_quality = _compute_trend_quality(
                snapshot, None, volume_pressure, obv_slope,
            )
            return BiasResult(
                MarketBias.NEUTRAL, adx, atr, volatility,
                f"ADX={adx:.1f}>{adx_threshold:.1f} but EMAs converged "
                f"(spread={ema_spread:.2f} < 0.15*ATR={0.15*atr:.2f})",
                volume_pressure=volume_pressure,
                trend_quality=trend_quality,
            )

        if ema_9 > ema_21:
            direction = "BULLISH"
            bias = MarketBias.BULLISH
        else:
            direction = "BEARISH"
            bias = MarketBias.BEARISH

        trend_quality = _compute_trend_quality(
            snapshot, direction, volume_pressure, obv_slope,
        )

        return BiasResult(
            bias, adx, atr, volatility,
            f"EMA9 {'>' if bias == MarketBias.BULLISH else '<'} EMA21, "
            f"ADX={adx:.1f}>{adx_threshold:.1f}, TQ={trend_quality:.2f}",
            volume_pressure=volume_pressure,
            trend_quality=trend_quality,
        )

    # ------------------------------------------------------------------
    # 3. NEUTRAL – low ADX or ambiguous
    # ------------------------------------------------------------------
    trend_quality = _compute_trend_quality(
        snapshot, None, volume_pressure, obv_slope,
    )
    return BiasResult(
        MarketBias.NEUTRAL, adx, atr, volatility,
        f"ADX={adx:.1f} below threshold ({adx_threshold:.1f})",
        volume_pressure=volume_pressure,
        trend_quality=trend_quality,
    )


def _check_blocked(
    adx: float,
    adx_roc: float | None,
    snapshot: dict,
) -> BiasResult | None:
    """
    BLOCKED: extreme acceleration or fat-tail conditions.

    Triggers:
    1. Return kurtosis > 5.5 (fat tails = unpredictable distribution)
       Note: moderate fat tails (4-5.5) are handled by SL-widening in trade_validator.
    2. ADX > 35 AND rising fast with volume/BB confirmation
    """
    # Fat tail check (from returns distribution)
    kurtosis = snapshot.get("return_kurtosis", 0.0) or 0.0
    if kurtosis > 5.5:
        return BiasResult(
            MarketBias.BLOCKED, adx, 0.0, "extreme",
            f"Fat tails: kurtosis={kurtosis:.1f} (>5.5)",
            blocked_reason="kurtosis",
        )

    # ADX acceleration check
    if adx >= 35:
        volume_ratio = snapshot.get("volume_ma_ratio")
        bb_width = snapshot.get("bb_width")
        bb_width_ma = snapshot.get("bb_width_ma")

        adx_rising_fast = adx_roc is not None and adx_roc > 2.5
        volume_spike = volume_ratio is not None and volume_ratio > 2.0
        bb_expanding = (
            bb_width is not None and bb_width_ma is not None
            and bb_width > bb_width_ma * 1.3
        )

        if adx_rising_fast and (volume_spike or bb_expanding):
            return BiasResult(
                MarketBias.BLOCKED, adx, 0.0, "extreme",
                f"ADX={adx:.1f} rising fast (ROC={adx_roc:.1f}), "
                f"vol={volume_ratio:.1f}x, BB_expand={bb_expanding}",
                blocked_reason="adx_acceleration",
            )

    return None


def _classify_volatility(atr: float, atr_avg: float | None) -> str:
    """Classify volatility from ATR ratio vs rolling average."""
    if atr_avg is None or atr_avg == 0:
        return "normal"

    ratio = atr / atr_avg

    if ratio > 2.0:
        return "extreme"
    elif ratio > 1.4:
        return "high"
    elif ratio < 0.6:
        return "low"
    return "normal"


def _classify_volume_pressure(snapshot: dict) -> str:
    """
    Classify buying/selling pressure from normalized volume delta.
    """
    vdr = snapshot.get("volume_delta_ratio", 0.0) or 0.0

    if vdr > 0.3:
        return "buying"
    elif vdr < -0.3:
        return "selling"
    return "neutral"


def _compute_trend_quality(
    snapshot: dict,
    direction: str | None,
    volume_pressure: str,
    obv_slope: float,
) -> float:
    """
    Compute trend quality as agreement score (0-1) between:
    1. EMA alignment (EMA9 vs EMA21 vs EMA50)  – weight 0.3
    2. ADX strength relative to median           – weight 0.2
    3. Volume delta direction                    – weight 0.2
    4. OBV slope direction                       – weight 0.2

    Higher = cleaner trend, more reliable for directional trades.
    """
    if direction is None:
        return 0.0

    score = 0.0

    # 1. EMA alignment (0.3 + 0.1 bonus for EMA50)
    ema_9 = snapshot.get("ema_9")
    ema_21 = snapshot.get("ema_21")
    ema_50 = snapshot.get("ema_50")
    if ema_9 is not None and ema_21 is not None:
        if direction == "BULLISH" and ema_9 > ema_21:
            score += 0.3
            if ema_50 is not None and ema_21 > ema_50:
                score += 0.1
        elif direction == "BEARISH" and ema_9 < ema_21:
            score += 0.3
            if ema_50 is not None and ema_21 < ema_50:
                score += 0.1

    # 2. ADX strength (0.2)
    adx = snapshot.get("adx", 0)
    adx_median = snapshot.get("adx_median", 18)
    if adx and adx_median and adx_median > 0:
        ratio = adx / adx_median
        if ratio > 1.0:
            score += min(0.2, 0.2 * (ratio - 1.0))

    # 3. Volume delta in trend direction (0.2)
    if direction == "BULLISH" and volume_pressure == "buying":
        score += 0.2
    elif direction == "BEARISH" and volume_pressure == "selling":
        score += 0.2

    # 4. OBV slope in trend direction (0.2)
    if direction == "BULLISH" and obv_slope > 0:
        score += 0.2
    elif direction == "BEARISH" and obv_slope < 0:
        score += 0.2

    return min(1.0, score)

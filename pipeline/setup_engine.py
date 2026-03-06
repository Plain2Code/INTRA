"""
Signal engine – Schicht 2.

Detects entry signals from the current 1min feature snapshot using
market bias for directional context.  Returns a SetupResult or None.

Two signal types:
  Noise Breakout  – price breaks beyond noise boundary (Zarattini et al. 2024)
  EMA Pullback    – buying dips in confirmed trends

Confidence-based signals:
  Each signal returns a confidence score (0.0 to 1.0) computed as a
  weighted average of confirmation factors.  Minimum confidence = 0.5.
  No hardcoded thresholds – uses percentile-based adaptive thresholds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import config

logger = logging.getLogger(__name__)


class SetupType(Enum):
    NOISE_BREAKOUT = "noise_breakout"


class Direction(Enum):
    LONG = "BUY"
    SHORT = "SELL"


@dataclass
class SetupResult:
    setup_type: SetupType
    direction: Direction
    confidence: float        # 0.0 to 1.0 (replaces score/max_score)
    details: list[str]       # human-readable list of conditions met


def detect_setup(
    snapshot_1min: dict[str, Any],
    last_close: float,
) -> SetupResult | None:
    """
    Detect a Noise Breakout signal based on 1min indicators.

    Pure mechanical signal – no bias filter (paper-conformant).
    """
    if not snapshot_1min.get("ready", False):
        return None

    return _check_noise_breakout(snapshot_1min, last_close)


# ---------------------------------------------------------------------------
# Signal A: Noise Boundary Momentum (Zarattini, Aziz & Barbon 2024)
# ---------------------------------------------------------------------------

def _check_noise_breakout(
    snap: dict, close: float,
) -> SetupResult | None:
    """
    Price breaks beyond noise boundary around daily open.

    Noise boundary = daily_open ± avg(|daily_return|, 14 days).
    Breakout above upper → LONG, below lower → SHORT.
    Only fires at :00 and :30 (checked by orchestrator before calling).

    Confidence factors (weighted, paper-conformant – no bias):
      - Breakout strength (distance beyond boundary)  (0.40)
      - Volume confirmation (volume_ma_ratio)          (0.30)
      - MACD histogram in breakout direction           (0.15)
      - ADX trending (above median)                    (0.15)
    """
    noise_upper = snap.get("noise_upper")
    noise_lower = snap.get("noise_lower")
    daily_open = snap.get("daily_open")
    atr = snap.get("atr")

    if noise_upper is None or noise_lower is None or daily_open is None:
        return None
    if atr is None or atr == 0:
        return None

    noise_width = snap.get("noise_boundary_width", 0)
    if noise_width is None or noise_width == 0:
        return None

    # Check breakout direction
    breakout_above = close > noise_upper
    breakout_below = close < noise_lower

    if not breakout_above and not breakout_below:
        return None

    # Minimum breakout strength in ATR units
    if breakout_above:
        breakout_dist = close - noise_upper
        direction = Direction.LONG
    else:
        breakout_dist = noise_lower - close
        direction = Direction.SHORT

    breakout_atr = breakout_dist / atr
    if breakout_atr < config.NOISE_MIN_BREAKOUT_ATR:
        return None

    confidence = 0.0
    details = [
        f"Noise breakout {'LONG' if breakout_above else 'SHORT'}: "
        f"close={close:.2f} boundary={noise_upper:.2f}/{noise_lower:.2f} "
        f"(open={daily_open:.2f} ± {noise_width:.2f})"
    ]

    # 1. Breakout strength (0.40) – scales from min threshold to 1.5 ATR
    strength_factor = min(1.0, (breakout_atr - config.NOISE_MIN_BREAKOUT_ATR) / 1.2)
    confidence += 0.40 * strength_factor
    details.append(f"Breakout strength: {breakout_atr:.2f} ATR (factor={strength_factor:.2f})")

    # 2. Volume confirmation (0.30) – volume above average = momentum
    vol_ratio = snap.get("volume_ma_ratio")
    vol_conf = 0.0
    if vol_ratio is not None and vol_ratio > 1.0:
        vol_conf = min(1.0, (vol_ratio - 1.0) / 1.0)  # scales 1.0-2.0x
        details.append(f"Volume {vol_ratio:.1f}x average")
    confidence += 0.30 * vol_conf

    # 3. MACD histogram in breakout direction (0.15)
    macd_hist = snap.get("macd_hist")
    macd_conf = 0.0
    if macd_hist is not None:
        if direction == Direction.LONG and macd_hist > 0:
            macd_conf = 1.0
            details.append(f"MACD hist positive ({macd_hist:.4f})")
        elif direction == Direction.SHORT and macd_hist < 0:
            macd_conf = 1.0
            details.append(f"MACD hist negative ({macd_hist:.4f})")
    confidence += 0.15 * macd_conf

    # 4. ADX above median (0.15) – trending market favors breakout
    adx_1min = snap.get("adx")
    adx_median = snap.get("adx_median", 20)
    adx_conf = 0.0
    if adx_1min is not None and adx_median and adx_median > 0:
        if adx_1min > adx_median:
            adx_conf = min(1.0, (adx_1min - adx_median) / adx_median)
            details.append(f"ADX trending ({adx_1min:.1f} > median {adx_median:.1f})")
    confidence += 0.15 * adx_conf

    confidence = max(0.0, min(1.0, confidence))

    if confidence >= config.MIN_CONFIDENCE:
        return SetupResult(
            SetupType.NOISE_BREAKOUT, direction,
            confidence, details,
        )
    return None


# ---------------------------------------------------------------------------
# Detailed condition scan for dashboard pipeline matrix
# ---------------------------------------------------------------------------

def scan_all_conditions(
    snapshot_1min: dict[str, Any],
    last_close: float,
) -> dict:
    """
    Scan all signal conditions and return detailed status for dashboard.
    Always returns results regardless of confidence.
    """
    if not snapshot_1min.get("ready", False):
        return {"ready": False, "reason": "1min indicators not ready"}

    noise_scan = _scan_noise_breakout(snapshot_1min, last_close)

    return {
        "ready": True,
        "signals": {
            "noise_breakout": noise_scan,
        },
    }


def _scan_noise_breakout(snap: dict, close: float) -> dict:
    """Scan Noise Breakout conditions with actual weighted confidence."""
    noise_upper = snap.get("noise_upper")
    noise_lower = snap.get("noise_lower")
    daily_open = snap.get("daily_open")
    atr = snap.get("atr")
    noise_width = snap.get("noise_boundary_width")

    if noise_upper is None or noise_lower is None or atr is None or atr == 0:
        return {"confidence": 0, "min": config.MIN_CONFIDENCE,
                "direction": "N/A", "conditions": []}

    # Determine breakout
    breakout_above = close > noise_upper
    breakout_below = close < noise_lower

    if breakout_above:
        direction = "LONG"
        breakout_dist = close - noise_upper
    elif breakout_below:
        direction = "SHORT"
        breakout_dist = noise_lower - close
    else:
        direction = "LONG" if close >= (daily_open or close) else "SHORT"
        breakout_dist = 0.0

    breakout_atr = breakout_dist / atr if atr > 0 else 0
    gate_pass = (breakout_above or breakout_below) and breakout_atr >= config.NOISE_MIN_BREAKOUT_ATR

    # --- Weighted confidence (mirrors _check_noise_breakout exactly) ---
    confidence = 0.0

    # 1. Breakout strength (0.40)
    strength_factor = min(1.0, (breakout_atr - config.NOISE_MIN_BREAKOUT_ATR) / 1.2) if gate_pass else 0.0
    confidence += 0.40 * strength_factor

    # 2. Volume confirmation (0.30)
    vol_ratio = snap.get("volume_ma_ratio")
    vol_pass = vol_ratio is not None and vol_ratio > 1.0
    vol_conf = min(1.0, (vol_ratio - 1.0) / 1.0) if vol_pass else 0.0
    confidence += 0.30 * vol_conf

    # 3. MACD histogram (0.15)
    macd_hist = snap.get("macd_hist")
    macd_pass = False
    if macd_hist is not None:
        if direction == "LONG" and macd_hist > 0:
            macd_pass = True
        elif direction == "SHORT" and macd_hist < 0:
            macd_pass = True
    confidence += 0.15 * (1.0 if macd_pass else 0.0)

    # 4. ADX above median (0.15)
    adx_1min = snap.get("adx")
    adx_median = snap.get("adx_median", 20)
    adx_pass = False
    adx_conf = 0.0
    if adx_1min is not None and adx_median and adx_median > 0:
        if adx_1min > adx_median:
            adx_conf = min(1.0, (adx_1min - adx_median) / adx_median)
            adx_pass = True
    confidence += 0.15 * adx_conf

    confidence = max(0.0, min(1.0, confidence))

    if not gate_pass:
        confidence = 0.0

    conds = [
        {"name": "Breakout", "pass": gate_pass, "gate": True,
         "val": f"{breakout_atr:.2f} ATR ({'above' if breakout_above else 'below' if breakout_below else 'inside'})"},
        {"name": "Boundary", "pass": noise_width is not None and noise_width > 0,
         "val": f"{noise_upper:.1f}/{noise_lower:.1f}" if noise_upper else "n/a"},
        {"name": "Volume", "pass": vol_pass,
         "val": f"{vol_ratio:.1f}x" if vol_ratio else "n/a"},
        {"name": "MACD hist", "pass": macd_pass,
         "val": f"{macd_hist:.4f}" if macd_hist else "n/a"},
        {"name": "ADX > med", "pass": adx_pass,
         "val": f"{adx_1min:.1f}/{adx_median:.1f}" if adx_1min else "n/a"},
    ]

    return {"confidence": round(confidence, 2), "min": config.MIN_CONFIDENCE,
            "direction": direction, "conditions": conds}



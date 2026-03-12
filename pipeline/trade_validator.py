"""
Trade validator – Schicht 3.

Computes ATR-based SL level and runs spread filter.
No fixed TP — exits are managed by trailing stop and EOD close (paper-conformant).

SL computation:
  - Base SL = FALLBACK_SL_ATR_MULT * ATR(1min)
  - Kurtosis widening: if return_kurtosis > 3, widen SL
  - Continuous volatility adjustment: scale by ATR/ATR_avg (clamped 0.7-1.5)

EV gating (post-bootstrap only):
  - Post-bootstrap: check spread-adjusted EV > 0 from stats

Hard filter:
  - SL must be >= MIN_SL_SPREAD_MULT * spread
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pipeline.setup_engine import SetupType, SetupResult, Direction
from core.statistics import StatisticsEngine
import config

logger = logging.getLogger(__name__)


@dataclass
class TradeSetup:
    sl_price: float         # absolute SL price
    tp_price: float         # absolute TP price (safety net, very wide)
    sl_distance: float      # SL distance in price units (used by risk_manager)
    tp_distance: float      # TP distance in price units (nominal, for stats only)
    rrr: float              # nominal RRR (for logging only)
    passes_filter: bool     # True if trade passes all checks
    reject_reason: str      # why it was rejected (empty if passes)
    entry_price: float = 0.0


def validate_trade(
    setup: SetupResult,
    atr: float,
    current_price: float,
    spread: float,
    volatility: str = "normal",
    stats: StatisticsEngine | None = None,
    return_kurtosis: float = 3.0,
    epic: str | None = None,
    vwap: float | None = None,
    atr_avg: float | None = None,
    noise_boundary: float | None = None,
) -> TradeSetup:
    """
    Calculate SL level and validate the trade.

    No fixed TP — the trailing stop and EOD close handle exits.
    A wide safety-net TP (10x ATR) is set as Capital.com backstop only.
    """
    # --- Compute SL distance (fixed ATR-based) ---
    sl_distance = atr * config.FALLBACK_SL_ATR_MULT

    # Kurtosis adjustment: widen SL for fat-tailed distributions
    if return_kurtosis > 3.0:
        kurtosis_mult = 1 + (return_kurtosis - 3) * 0.1
        sl_distance *= kurtosis_mult

    # Continuous volatility adjustment: scale by ATR/ATR_avg ratio
    if atr_avg is not None and atr_avg > 0:
        vol_mult = max(0.7, min(1.5, atr / atr_avg))
        sl_distance *= vol_mult
    elif volatility in ("high", "extreme"):
        sl_distance *= 1.3
    elif volatility == "low":
        sl_distance *= 0.8

    # Safety-net TP (very wide, not a real target)
    tp_distance = atr * 10.0

    # --- Entry price estimation (ASK for LONG, BID for SHORT) ---
    half_spread = spread / 2.0
    if setup.direction == Direction.LONG:
        entry_price = current_price + half_spread
        sl_price = entry_price - sl_distance
        tp_price = entry_price + tp_distance
    else:
        entry_price = current_price - half_spread
        sl_price = entry_price + sl_distance
        tp_price = entry_price - tp_distance

    rrr = tp_distance / sl_distance if sl_distance > 0 else 0.0

    # --- Hard filter: SL must be wide enough relative to spread ---
    if spread > 0 and sl_distance < spread * config.MIN_SL_SPREAD_MULT:
        return TradeSetup(
            sl_price=sl_price, tp_price=tp_price,
            sl_distance=sl_distance, tp_distance=tp_distance,
            rrr=rrr, passes_filter=False,
            reject_reason=(
                f"SL too tight: {sl_distance:.4f} < spread({spread:.4f}) x "
                f"{config.MIN_SL_SPREAD_MULT} = {spread * config.MIN_SL_SPREAD_MULT:.4f}"
            ),
            entry_price=entry_price,
        )

    # --- Post-bootstrap EV gate (uses actual PnL-based EV from stats) ---
    setup_type = setup.setup_type.value

    if stats is not None and not stats.should_trade(setup_type, epic=epic):
        epic_s = stats.get_stats(setup_type, epic) if epic else None
        if epic_s and epic_s.total >= config.PER_EPIC_MIN_TRADES:
            s = epic_s
            label = f"{setup_type}:{epic}"
        else:
            s = stats.get_stats(setup_type)
            label = setup_type
        return TradeSetup(
            sl_price=sl_price, tp_price=tp_price,
            sl_distance=sl_distance, tp_distance=tp_distance,
            rrr=rrr, passes_filter=False,
            reject_reason=(
                f"Stats engine blocks {label}: EV={s.expected_value:.2f} "
                f"(WR={s.winrate:.0%} AvgW={s.avg_win:.2f} AvgL={s.avg_loss:.2f})"
            ),
            entry_price=entry_price,
        )

    if stats is not None and stats.get_total_trades(setup_type) >= config.BOOTSTRAP_MIN_TRADES:
        s = stats.get_stats(setup_type)
        logger.debug("EV PASS: %s EV=%.2f WR=%.0f%%", setup_type, s.expected_value, s.winrate * 100)

    # --- All checks passed ---
    logger.debug("Validation passed: %s %s SL=%.2f spread=%.4f",
                 setup.setup_type.value, setup.direction.value, sl_price, spread)

    return TradeSetup(
        sl_price=sl_price, tp_price=tp_price,
        sl_distance=sl_distance, tp_distance=tp_distance,
        rrr=rrr, passes_filter=True, reject_reason="",
        entry_price=entry_price,
    )

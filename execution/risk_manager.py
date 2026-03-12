"""
Risk manager – position sizing with half-Kelly and correlation adjustment.

Uses StatisticsEngine for adaptive risk:
  - Half-Kelly when enough data (>= BOOTSTRAP_MIN_TRADES)
  - Base risk during bootstrap phase
  - Minimal risk when edge is negative
  - Correlation-adjusted scaling for correlated exposure

No hardcoded loss multipliers – Kelly adapts to changing win rates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.statistics import StatisticsEngine
import config

logger = logging.getLogger(__name__)


@dataclass
class PositionSize:
    size: float
    risk_amount: float        # target risk amount (balance * risk_pct)
    sl_distance: float        # in price units
    effective_risk: float = 0.0   # actual EUR at risk (size * sl_distance)
    skip: bool = False            # True if effective risk too small


def calculate_position_size(
    balance: float,
    sl_distance: float,
    current_price: float = 0.0,
    min_size: float = 0.01,
    max_leverage: float | None = None,
    stats: StatisticsEngine | None = None,
    setup_type: str = "",
    open_positions: dict[str, Any] | None = None,
) -> PositionSize:
    """
    Calculate position size with half-Kelly sizing and correlation adjustment.

    Args:
        balance: Current account balance
        sl_distance: Stop loss distance in price units
        current_price: Current market price (for leverage cap)
        min_size: Minimum allowed position size
        max_leverage: Per-instrument leverage override
        stats: StatisticsEngine for adaptive risk (None = use base risk)
        setup_type: Setup type string for stats lookup
        open_positions: Dict of epic -> PositionMeta for correlation check
    """
    if sl_distance <= 0:
        logger.warning("Invalid SL distance: %.4f", sl_distance)
        return PositionSize(min_size, 0.0, sl_distance)

    # --- Risk percentage from statistics engine ---
    if stats is not None and setup_type:
        risk_pct = stats.get_risk_pct(setup_type)
    else:
        risk_pct = config.RISK_PER_TRADE_PCT

    # --- Correlation adjustment ---
    if stats is not None and open_positions and len(open_positions) > 0:
        eff_positions = stats.effective_position_count(open_positions)
        if eff_positions >= 2.0:
            scale = 2.0 / eff_positions
            risk_pct *= scale
            logger.debug("Correlation adj: %.1f eff pos → risk=%.2f%%", eff_positions, risk_pct * 100)

    risk_amount = balance * risk_pct
    raw_size = risk_amount / sl_distance

    # --- Leverage cap ---
    leverage_limit = max_leverage if max_leverage is not None else config.MAX_TRADE_LEVERAGE
    was_capped = False
    if current_price > 0:
        max_notional = balance * leverage_limit
        max_size_by_leverage = max_notional / current_price
        size = max(min_size, min(raw_size, max_size_by_leverage))
        was_capped = raw_size > max_size_by_leverage
    else:
        size = max(min_size, raw_size)

    # Round to 2 decimal places (standard lot precision)
    size = round(size, 2)

    effective_risk = size * sl_distance
    effective_risk_pct = effective_risk / balance if balance > 0 else 0.0

    # Minimum effective risk check
    min_effective = risk_amount * config.MIN_EFFECTIVE_RISK_RATIO
    skip = was_capped and effective_risk < min_effective

    if skip:
        logger.debug("Position SKIP: risk %.2f€ below min %.2f€", effective_risk, min_effective)
    elif was_capped:
        logger.debug("Position capped: size %.2f, risk %.2f€ (%.1f%%)", size, effective_risk, effective_risk_pct * 100)

    logger.debug("Sizing: size=%.2f risk=%.2f€ (%.2f%%)", size, effective_risk, effective_risk_pct * 100)

    return PositionSize(
        size=size,
        risk_amount=risk_amount,
        sl_distance=sl_distance,
        effective_risk=effective_risk,
        skip=skip,
    )

"""
Statistical engine – adaptive parameter computation.

Central module: computes EV, Kelly fraction, optimal trailing distance,
and correlation-adjusted position limits from tracked trade data.

All trading parameters flow through this engine instead of being hardcoded.
Replaces magic numbers with data-driven values.
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade statistics per bucket (setup_type or setup_type:epic)
# ---------------------------------------------------------------------------

@dataclass
class TradeStatistics:
    """Rolling statistics computed from recent trades."""
    wins: int = 0
    losses: int = 0
    _pnl_list: deque = field(default_factory=lambda: deque(maxlen=200))
    _win_amounts: deque = field(default_factory=lambda: deque(maxlen=200))
    _loss_amounts: deque = field(default_factory=lambda: deque(maxlen=200))
    _hold_times_win: deque = field(default_factory=lambda: deque(maxlen=200))
    _hold_times_all: deque = field(default_factory=lambda: deque(maxlen=200))
    _sl_distances: deque = field(default_factory=lambda: deque(maxlen=200))
    _tp_distances: deque = field(default_factory=lambda: deque(maxlen=200))
    _win_distances: deque = field(default_factory=lambda: deque(maxlen=200))

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def winrate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0

    @property
    def avg_win(self) -> float:
        return sum(self._win_amounts) / len(self._win_amounts) if self._win_amounts else 0.0

    @property
    def avg_loss(self) -> float:
        return sum(self._loss_amounts) / len(self._loss_amounts) if self._loss_amounts else 0.0

    @property
    def profit_factor(self) -> float:
        total_loss = sum(self._loss_amounts)
        if total_loss == 0:
            return float('inf') if self._win_amounts else 0.0
        return sum(self._win_amounts) / total_loss

    @property
    def expected_value(self) -> float:
        """EV = WR × AvgWin - (1-WR) × AvgLoss. In price-unit terms."""
        if self.total == 0:
            return 0.0
        wr = self.winrate
        return wr * self.avg_win - (1 - wr) * self.avg_loss

    @property
    def kelly_fraction(self) -> float:
        """
        Kelly criterion: f* = (p × b - q) / b
        where p = win rate, q = 1-p, b = avg_win / avg_loss.
        Returns 0 if edge is negative or insufficient data.
        """
        if self.total < 10 or self.avg_loss == 0:
            return 0.0
        p = self.winrate
        q = 1 - p
        b = self.avg_win / self.avg_loss
        kelly = (p * b - q) / b
        return max(0.0, kelly)

    @property
    def optimal_hold_minutes(self) -> float:
        """Median hold time of winning trades."""
        if not self._hold_times_win:
            return 15.0  # sensible default
        sorted_times = sorted(self._hold_times_win)
        mid = len(sorted_times) // 2
        return sorted_times[mid]

    @property
    def optimal_trail_distance_r(self) -> float:
        """
        Optimal trailing distance as fraction of SL distance.
        Computed from the distribution of winning trade PnL:
        trail at the 30th percentile of win amounts (captures most wins
        while giving room to run).
        """
        if len(self._win_amounts) < 5:
            return 2.0  # fallback: 2× SL distance
        sorted_wins = sorted(self._win_amounts)
        # 30th percentile: tight enough to capture, wide enough to not chop
        idx = max(0, int(len(sorted_wins) * 0.3) - 1)
        p30_win = sorted_wins[idx]
        if self.avg_loss > 0:
            return max(1.5, p30_win / self.avg_loss)
        return 2.0

    def record(
        self,
        pnl: float,
        hold_minutes: float = 0.0,
        sl_distance: float = 0.0,
        tp_distance: float = 0.0,
        win_distance: float = 0.0,
    ):
        """Record a completed trade. Scratch trades (< 0.3R) are tracked but don't affect win/loss stats."""
        self._pnl_list.append(pnl)
        self._hold_times_all.append(hold_minutes)
        if sl_distance > 0:
            self._sl_distances.append(sl_distance)
        if tp_distance > 0:
            self._tp_distances.append(tp_distance)

        # Scratch zone: trades with |PnL| < 0.3 × SL_distance are noise
        # They get recorded in PnL history but don't count as win or loss
        if sl_distance > 0 and abs(pnl) < 0.3 * sl_distance:
            logger.debug("Scratch trade: PnL=%.2f (< 0.3 × SL=%.2f) – not counted in W/L",
                         pnl, sl_distance)
            return

        if pnl > 0:
            self.wins += 1
            self._win_amounts.append(abs(pnl))
            self._hold_times_win.append(hold_minutes)
            if win_distance > 0:
                self._win_distances.append(win_distance)
        else:
            self.losses += 1
            self._loss_amounts.append(abs(pnl))

    def to_dict(self) -> dict:
        return {
            "wins": self.wins,
            "losses": self.losses,
            "pnl_list": list(self._pnl_list),
            "win_amounts": list(self._win_amounts),
            "loss_amounts": list(self._loss_amounts),
            "hold_times_win": list(self._hold_times_win),
            "hold_times_all": list(self._hold_times_all),
            "sl_distances": list(self._sl_distances),
            "tp_distances": list(self._tp_distances),
            "win_distances": list(self._win_distances),
        }

    @classmethod
    def from_dict(cls, d: dict) -> TradeStatistics:
        ts = cls()
        ts.wins = d.get("wins", 0)
        ts.losses = d.get("losses", 0)
        for v in d.get("pnl_list", []):
            ts._pnl_list.append(v)
        for v in d.get("win_amounts", []):
            ts._win_amounts.append(v)
        for v in d.get("loss_amounts", []):
            ts._loss_amounts.append(v)
        for v in d.get("hold_times_win", []):
            ts._hold_times_win.append(v)
        for v in d.get("hold_times_all", []):
            ts._hold_times_all.append(v)
        for v in d.get("sl_distances", []):
            ts._sl_distances.append(v)
        for v in d.get("tp_distances", []):
            ts._tp_distances.append(v)
        for v in d.get("win_distances", []):
            ts._win_distances.append(v)
        return ts


# ---------------------------------------------------------------------------
# Correlation tracker
# ---------------------------------------------------------------------------

class CorrelationTracker:
    """
    Track return correlations between instruments from live candle data.
    Uses rolling Pearson correlation over a window of 1-minute returns.
    Falls back to static correlation matrix from config when insufficient data.
    """

    def __init__(self, instruments: list[str], window: int = 120):
        self._instruments = instruments
        self._window = window
        self._returns: dict[str, deque] = {
            epic: deque(maxlen=window) for epic in instruments
        }

    def update(self, epic: str, ret: float):
        """Record a 1-minute return for an instrument."""
        if epic in self._returns:
            self._returns[epic].append(ret)

    def get_correlation(self, epic_a: str, epic_b: str) -> float:
        """
        Get correlation between two instruments.
        Uses live data if enough samples, otherwise falls back to static matrix.
        """
        if epic_a == epic_b:
            return 1.0

        ra = self._returns.get(epic_a)
        rb = self._returns.get(epic_b)

        # Need at least 30 paired observations
        if ra and rb and len(ra) >= 30 and len(rb) >= 30:
            n = min(len(ra), len(rb))
            a_list = list(ra)[-n:]
            b_list = list(rb)[-n:]
            corr = self._pearson(a_list, b_list)
            if corr is not None:
                return corr

        # Fallback to static matrix
        static = config.CORRELATION_MATRIX
        if epic_a in static and epic_b in static[epic_a]:
            return static[epic_a][epic_b]
        return 0.0

    def effective_position_count(
        self,
        open_positions: dict[str, Any],
    ) -> float:
        """
        Compute effective number of positions accounting for correlation.

        For N positions with pairwise correlations ρ_ij, the effective count
        is: sqrt(sum_i sum_j ρ_ij) which equals sqrt(N + 2 * sum_{i<j} ρ_ij).

        3 perfectly correlated positions → 3.0
        3 uncorrelated positions → 1.73
        3 positions with ρ=0.85 → ~2.7
        """
        if len(open_positions) <= 1:
            return float(len(open_positions))

        epics = list(open_positions.keys())
        n = len(epics)

        # Build correlation sum
        total = 0.0
        for i in range(n):
            for j in range(n):
                # Weight by direction: same direction = positive correlation effect
                dir_i = open_positions[epics[i]].direction if hasattr(open_positions[epics[i]], 'direction') else "BUY"
                dir_j = open_positions[epics[j]].direction if hasattr(open_positions[epics[j]], 'direction') else "BUY"
                corr = self.get_correlation(epics[i], epics[j])
                # Opposite directions flip correlation sign
                if dir_i != dir_j:
                    corr = -corr
                total += corr

        return math.sqrt(max(total, 1.0))

    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float | None:
        """Pearson correlation coefficient."""
        n = len(a)
        if n < 5:
            return None
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        var_a = sum((x - mean_a) ** 2 for x in a)
        var_b = sum((x - mean_b) ** 2 for x in b)
        if var_a == 0 or var_b == 0:
            return None
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
        return cov / math.sqrt(var_a * var_b)


# ---------------------------------------------------------------------------
# Statistics engine (central hub)
# ---------------------------------------------------------------------------

class StatisticsEngine:
    """
    Central hub for all adaptive trading parameters.
    Orchestrator calls this for EV, Kelly, trailing distance, etc.

    Maintains per-setup-type statistics and correlation tracking.
    Persists to stats.json for survival across restarts.
    """

    def __init__(self, instruments: list[str] | None = None):
        self._stats: dict[str, TradeStatistics] = {}
        self._correlation = CorrelationTracker(instruments or [])

    def record_trade(
        self,
        setup_type: str,
        epic: str,
        pnl: float,
        hold_minutes: float = 0.0,
        sl_distance: float = 0.0,
        tp_distance: float = 0.0,
    ):
        """Record a completed trade in all relevant buckets."""
        # Global bucket (all epics pooled per setup type)
        self._get_or_create(setup_type).record(
            pnl, hold_minutes, sl_distance, tp_distance,
        )
        # Per-epic bucket
        epic_key = f"{setup_type}:{epic}"
        self._get_or_create(epic_key).record(
            pnl, hold_minutes, sl_distance, tp_distance,
        )

        # Log updated stats
        s = self._stats[setup_type]
        logger.info(
            "STATS [%s]: WR=%.0f%% (%d/%d) EV=%.2f PF=%.2f Kelly=%.3f AvgW=%.2f AvgL=%.2f",
            setup_type, s.winrate * 100, s.wins, s.total,
            s.expected_value, s.profit_factor, s.kelly_fraction,
            s.avg_win, s.avg_loss,
        )

    def update_correlation(self, epic: str, ret: float):
        """Feed per-candle return to correlation tracker."""
        self._correlation.update(epic, ret)

    # --- Query methods ---

    def get_stats(self, setup_type: str, epic: str | None = None) -> TradeStatistics:
        """Get statistics for a setup type, optionally per-epic."""
        key = f"{setup_type}:{epic}" if epic else setup_type
        return self._stats.get(key, TradeStatistics())

    def get_ev(self, setup_type: str) -> float:
        """Expected value for a setup type (all epics pooled)."""
        return self.get_stats(setup_type).expected_value

    def get_winrate(self, setup_type: str) -> float:
        """Win rate for a setup type."""
        return self.get_stats(setup_type).winrate

    def get_total_trades(self, setup_type: str) -> int:
        """Total number of recorded trades for this setup type."""
        return self.get_stats(setup_type).total

    def get_kelly_fraction(self, setup_type: str) -> float:
        """Kelly fraction for position sizing. Returns 0 if insufficient data."""
        s = self.get_stats(setup_type)
        if s.total < config.KELLY_RAMP_START:
            return 0.0  # not enough data for stable Kelly estimate
        return s.kelly_fraction

    def should_trade(self, setup_type: str, epic: str | None = None) -> bool:
        """
        Returns True if we should take this trade type.
        - During bootstrap (<20 global trades): always True (learning phase)
        - After bootstrap: per-epic EV gate if enough per-epic data, else global EV

        Per-epic gating prevents losing instruments (e.g. GOLD ema_pullback)
        from dragging down profitable ones (e.g. DE40 ema_pullback).
        """
        global_stats = self.get_stats(setup_type)
        if global_stats.total < config.BOOTSTRAP_MIN_TRADES:
            return True  # bootstrap phase

        # Per-epic gate: if we have enough data for this specific epic, use it
        if epic:
            epic_stats = self.get_stats(setup_type, epic)
            if epic_stats.total >= config.PER_EPIC_MIN_TRADES:
                decision = epic_stats.expected_value > 0
                if not decision:
                    logger.info(
                        "EV BLOCK [%s:%s]: per-epic EV=%.2f (%d trades, WR=%.0f%%)",
                        setup_type, epic, epic_stats.expected_value,
                        epic_stats.total, epic_stats.winrate * 100,
                    )
                return decision

        # Fallback to global stats
        return global_stats.expected_value > 0

    def get_optimal_trail_atr_mult(self, setup_type: str) -> float:
        """
        Optimal trailing stop distance as ATR multiplier.
        Derived from the distribution of winning trades.
        Falls back to config floor during bootstrap.
        """
        s = self.get_stats(setup_type)
        if s.total < 10:
            return config.TRAILING_MIN_ATR_MULT
        # Use the ratio from realized trade data
        return max(config.TRAILING_MIN_ATR_MULT, s.optimal_trail_distance_r)

    def get_optimal_hold_minutes(self, setup_type: str) -> float:
        """Median hold time of winning trades for this setup type."""
        return self.get_stats(setup_type).optimal_hold_minutes

    def effective_position_count(self, open_positions: dict) -> float:
        """Correlation-adjusted position count."""
        return self._correlation.effective_position_count(open_positions)

    def get_risk_pct(self, setup_type: str) -> float:
        """
        Risk percentage for position sizing.
        Smooth ramp from base risk to half-Kelly over trades KELLY_RAMP_START..KELLY_RAMP_END.
        EV gating activates at BOOTSTRAP_MIN_TRADES (20), but Kelly sizing waits
        until KELLY_RAMP_START (50) for stable win-rate estimates.
        """
        s = self.get_stats(setup_type)

        # Negative edge after bootstrap → minimal risk
        if s.total >= config.BOOTSTRAP_MIN_TRADES and s.expected_value <= 0:
            return config.MIN_BOOTSTRAP_RISK_PCT

        kelly = self.get_kelly_fraction(setup_type)
        if kelly > 0 and s.total >= config.KELLY_RAMP_START:
            kelly_risk = min(kelly / 2, config.MAX_KELLY_RISK_PCT)
            # Smooth ramp: blend base risk → Kelly risk over KELLY_RAMP_START..KELLY_RAMP_END
            if s.total < config.KELLY_RAMP_END:
                ramp_range = config.KELLY_RAMP_END - config.KELLY_RAMP_START
                blend = (s.total - config.KELLY_RAMP_START) / ramp_range
                return config.RISK_PER_TRADE_PCT + blend * (kelly_risk - config.RISK_PER_TRADE_PCT)
            return kelly_risk

        return config.RISK_PER_TRADE_PCT  # base risk before Kelly ramp

    # --- Dashboard ---

    def get_all_stats_summary(self) -> dict[str, dict]:
        """Summary for dashboard display (includes per-epic buckets)."""
        result = {}
        for key, s in self._stats.items():
            result[key] = {
                "wins": s.wins,
                "losses": s.losses,
                "total": s.total,
                "winrate": round(s.winrate, 3),
                "avg_win": round(s.avg_win, 2),
                "avg_loss": round(s.avg_loss, 2),
                "ev": round(s.expected_value, 2),
                "profit_factor": round(s.profit_factor, 2) if s.profit_factor != float('inf') else 999.0,
                "kelly": round(s.kelly_fraction, 4),
                "optimal_hold_min": round(s.optimal_hold_minutes, 1),
            }
        return result

    def get_epic_stats_summary(self, epic: str) -> dict[str, dict]:
        """Per-epic stats for dashboard."""
        result = {}
        for key, s in self._stats.items():
            if key.endswith(f":{epic}"):
                setup = key.split(":")[0]
                result[setup] = {
                    "wins": s.wins,
                    "losses": s.losses,
                    "total": s.total,
                    "winrate": round(s.winrate, 3),
                    "ev": round(s.expected_value, 2),
                }
        return result

    # --- Persistence ---

    def save(self):
        """Persist statistics to stats.json."""
        data = {
            key: s.to_dict() for key, s in self._stats.items()
        }
        try:
            with open(config.STATS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save stats.json: %s", e)

    def load(self):
        """Load statistics from stats.json (silent if missing)."""
        import os
        if not os.path.exists(config.STATS_FILE):
            return
        try:
            with open(config.STATS_FILE) as f:
                data = json.load(f)
            for key, d in data.items():
                self._stats[key] = TradeStatistics.from_dict(d)
            logger.info(
                "Loaded stats for %d buckets from stats.json",
                len(self._stats),
            )
            # Log summary
            for key, s in self._stats.items():
                if ":" not in key:
                    logger.info(
                        "  %s: WR=%.0f%% (%d trades) EV=%.2f PF=%.2f Kelly=%.3f",
                        key, s.winrate * 100, s.total,
                        s.expected_value, s.profit_factor, s.kelly_fraction,
                    )
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load stats.json (starting fresh): %s", e)

    # --- Internal ---

    def _get_or_create(self, key: str) -> TradeStatistics:
        if key not in self._stats:
            self._stats[key] = TradeStatistics()
        return self._stats[key]

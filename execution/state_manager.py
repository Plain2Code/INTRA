"""
State manager – daily trading state in RAM.

Standalone module: tracks daily PnL, trade count, consecutive losses,
kill switch status, and active mode.  Resets at the start of each
trading day.  No persistence – all state reconstructed from API on
restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pipeline.regime_classifier import MarketBias, BiasResult
import config

logger = logging.getLogger(__name__)


@dataclass
class DailyState:
    date: str = ""
    starting_balance: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_peak: float = 0.0  # high water mark for drawdown calc
    trade_count: int = 0
    kill_switch: bool = False


class StateManager:
    """
    Manages all runtime state for the trading bot.

    Usage:
        sm = StateManager()
        sm.initialize(balance=10000.0)
        sm.record_trade(pnl=-50.0)  # loss
        sm.is_trading_allowed()  # checks all conditions
    """

    def __init__(self):
        self._daily = DailyState()
        self._is_live: bool = False
        self._is_running: bool = False
        # Per-epic bias tracking
        self._biases: dict[str, BiasResult] = {}
        self._active_instruments: list[str] = []
        self._current_balance: float = 0.0
        self._current_equity: float = 0.0
        self._margin_used: float = 0.0
        self._available: float = 0.0
        self._equity_history: list[dict] = []  # [{ts, equity}]
        # Per-epic last setup tracking
        self._last_setups: dict[str, dict] = {}
        self._last_setup_global: dict | None = None
        # Per-epic circuit breaker (consecutive SL hits)
        self._epic_consecutive_sl: dict[str, int] = {}
        self._epic_paused: set[str] = set()
        # Global consecutive losses (for adaptive risk sizing)
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, balance: float, is_live: bool = False):
        """Initialize state at bot startup."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily = DailyState(
            date=today,
            starting_balance=balance,
        )
        self._current_balance = balance
        self._current_equity = balance
        self._is_live = is_live
        logger.debug("State initialized: balance=%.2f mode=%s", balance, "LIVE" if is_live else "DEMO")

    def restore_daily(self, pnl: float, trade_count: int):
        """Restore daily P&L and trade count from persisted trades after restart."""
        self._daily.daily_pnl = pnl
        self._daily.daily_pnl_peak = max(pnl, 0.0)
        self._daily.trade_count = trade_count
        if pnl != 0 or trade_count != 0:
            logger.debug("Daily state restored: PnL=%.2f trades=%d", pnl, trade_count)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def check_day_reset(self):
        """Reset daily state if a new trading day has started."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily.date:
            logger.info("New trading day: %s → %s. Resetting daily state.",
                        self._daily.date, today)
            self._daily = DailyState(
                date=today,
                starting_balance=self._current_balance,
            )
            self._epic_consecutive_sl.clear()
            self._epic_paused.clear()
            self._consecutive_losses = 0

    # ------------------------------------------------------------------
    # Trade tracking
    # ------------------------------------------------------------------

    def record_trade(self, pnl: float, epic: str | None = None,
                     sl_distance: float = 0.0):
        """Record a completed trade's PnL and update per-epic circuit breaker.

        Scratch trades (|PnL| < 0.3 × SL_distance) are tracked in daily PnL
        but don't count toward circuit breaker or consecutive loss streaks.
        """
        self._daily.daily_pnl += pnl
        if self._daily.daily_pnl > self._daily.daily_pnl_peak:
            self._daily.daily_pnl_peak = self._daily.daily_pnl
        self._daily.trade_count += 1
        dd = self._daily.daily_pnl_peak - self._daily.daily_pnl
        logger.debug("State: trade %+.2f → daily PnL: %+.2f (peak: %+.2f, dd: %.2f, #%d)",
                     pnl, self._daily.daily_pnl, self._daily.daily_pnl_peak, dd, self._daily.trade_count)

        # Scratch zone: tiny PnL is noise, skip streak tracking
        if sl_distance > 0 and abs(pnl) < 0.3 * sl_distance:
            logger.debug("Scratch trade (%.2f < 0.3 × SL %.2f) – skipping streak tracking",
                         pnl, sl_distance)
            return

        # Global consecutive loss tracking (for adaptive risk)
        if pnl < 0:
            self._consecutive_losses += 1
            logger.debug("Consecutive losses: %d", self._consecutive_losses)
        else:
            if self._consecutive_losses > 0:
                logger.debug("Loss streak reset from %d", self._consecutive_losses)
            self._consecutive_losses = 0

        if epic is not None:
            if pnl < 0:
                count = self._epic_consecutive_sl.get(epic, 0) + 1
                self._epic_consecutive_sl[epic] = count
                logger.debug("Consecutive SL for %s: %d/%d",
                             epic, count, config.MAX_CONSECUTIVE_SL_PER_EPIC)
                if count >= config.MAX_CONSECUTIVE_SL_PER_EPIC:
                    self._epic_paused.add(epic)
                    logger.warning(
                        "CIRCUIT BREAKER: %s paused for rest of session "
                        "(%d consecutive SL hits)", epic, count,
                    )
            else:
                # Win resets the streak
                if epic in self._epic_consecutive_sl:
                    self._epic_consecutive_sl[epic] = 0

    def is_epic_paused(self, epic: str) -> bool:
        """Return True if this epic is paused due to consecutive SL circuit breaker."""
        return epic in self._epic_paused

    @property
    def consecutive_losses(self) -> int:
        """Global consecutive loss count (for adaptive risk sizing)."""
        return self._consecutive_losses

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def is_daily_loss_reached(self) -> bool:
        """Check if daily drawdown limit is reached (from peak, not just net P&L)."""
        if self._daily.starting_balance == 0:
            return False
        # Drawdown from intraday peak: if you made +200 then lost 600,
        # daily_pnl=-400, peak=+200, drawdown=600 → 600/balance = dd%
        drawdown = self._daily.daily_pnl_peak - self._daily.daily_pnl
        dd_pct = drawdown / self._daily.starting_balance
        return dd_pct >= config.MAX_DAILY_LOSS_PCT

    def is_kill_switch_active(self) -> bool:
        return self._daily.kill_switch

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def activate_kill_switch(self):
        self._daily.kill_switch = True
        logger.warning("KILL SWITCH ACTIVATED")

    def deactivate_kill_switch(self):
        self._daily.kill_switch = False
        logger.info("Kill switch deactivated")

    # ------------------------------------------------------------------
    # Balance / equity updates
    # ------------------------------------------------------------------

    def update_balance(self, balance: float, equity: float | None = None,
                       margin_used: float | None = None,
                       available: float | None = None):
        self._current_balance = balance
        if equity is not None:
            self._current_equity = equity
            # Record equity snapshot (max 1 per minute → ~480 per 8h session)
            now = datetime.now(timezone.utc).isoformat()
            if (not self._equity_history or
                    self._equity_history[-1]["ts"][:16] != now[:16]):
                self._equity_history.append({"ts": now, "equity": equity})
                # Keep last 1000 points
                if len(self._equity_history) > 1000:
                    self._equity_history = self._equity_history[-1000:]
        if margin_used is not None:
            self._margin_used = margin_used
        if available is not None:
            self._available = available

    # ------------------------------------------------------------------
    # Bias tracking
    # ------------------------------------------------------------------

    def set_bias(self, epic: str, bias_result: BiasResult):
        prev = self._biases.get(epic)
        if prev is None or prev.bias != bias_result.bias:
            logger.debug("Bias changed %s → %s", epic, bias_result.bias.value)
        self._biases[epic] = bias_result

    def get_bias(self, epic: str) -> BiasResult:
        return self._biases.get(
            epic,
            BiasResult(MarketBias.NEUTRAL, 0.0, 0.0, "normal", "No data"),
        )

    # ------------------------------------------------------------------
    # Running state
    # ------------------------------------------------------------------

    def set_running(self, running: bool):
        self._is_running = running

    def set_instruments(self, instruments: list[str]):
        self._active_instruments = instruments

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return self._is_live

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def balance(self) -> float:
        return self._current_balance

    @property
    def equity(self) -> float:
        return self._current_equity

    @property
    def available(self) -> float:
        return self._available

    @property
    def margin_used(self) -> float:
        return self._margin_used

    @property
    def daily_pnl(self) -> float:
        return self._daily.daily_pnl

    @property
    def daily_pnl_pct(self) -> float:
        if self._daily.starting_balance == 0:
            return 0.0
        return self._daily.daily_pnl / self._daily.starting_balance

    @property
    def daily_drawdown_pct(self) -> float:
        """Drawdown from intraday P&L peak as a fraction of starting balance."""
        if self._daily.starting_balance == 0:
            return 0.0
        drawdown = self._daily.daily_pnl_peak - self._daily.daily_pnl
        return drawdown / self._daily.starting_balance

    @property
    def trade_count(self) -> int:
        return self._daily.trade_count

    @property
    def active_instruments(self) -> list[str]:
        return self._active_instruments

    # ------------------------------------------------------------------
    # Last setup tracking
    # ------------------------------------------------------------------

    def set_last_setup(self, epic: str, info: dict):
        self._last_setups[epic] = info
        self._last_setup_global = info

    # ------------------------------------------------------------------
    # Snapshot for dashboard
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "mode": "LIVE" if self._is_live else "DEMO",
            "running": self._is_running,
            "balance": self._current_balance,
            "equity": self._current_equity,
            "margin_used": self._margin_used,
            "available": self._available,
            "daily_pnl": self._daily.daily_pnl,
            "daily_pnl_pct": self.daily_pnl_pct,
            "daily_drawdown_pct": self.daily_drawdown_pct,
            "trade_count": self._daily.trade_count,
            # Per-epic bias info
            "biases": {
                epic: {
                    "bias": br.bias.value,
                    "adx": round(br.adx, 1),
                    "volatility": br.volatility,
                    "details": br.details,
                    "volume_pressure": br.volume_pressure,
                    "trend_quality": round(br.trend_quality, 2),
                }
                for epic, br in self._biases.items()
            },
            "kill_switch": self._daily.kill_switch,
            "instruments": self._active_instruments,
            "date": self._daily.date,
            "equity_history": self._equity_history,
            "last_setup": self._last_setup_global,
            "last_setups": self._last_setups,
            "paused_epics": list(self._epic_paused),
            "epic_consecutive_sl": dict(self._epic_consecutive_sl),
            "consecutive_losses": self._consecutive_losses,
        }

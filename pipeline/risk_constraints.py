"""
Risk constraints – Schicht 4 pre-trade checks.

Standalone module: performs all pre-trade safety checks before the
decision pipeline runs.  Returns ConstraintResult(allowed, reason).

Checks (in order): kill switch, bot running, weekend, daily loss limit,
position already open, spread, session close buffer, news.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from execution.state_manager import StateManager
from core.news_filter import NewsFilter
import config

logger = logging.getLogger(__name__)


@dataclass
class ConstraintResult:
    allowed: bool
    reason: str  # empty if allowed, explains block otherwise


class RiskConstraints:
    """
    Pre-trade risk checks (Schicht 4).

    Checks are ordered by cost – cheapest first.
    """

    def __init__(
        self,
        state: StateManager,
        news_filter: NewsFilter,
    ):
        self._state = state
        self._news = news_filter

    def check_all(
        self,
        epic: str,
        current_spread: float,
        avg_spread: float,
        has_open_position: bool,
        total_open_positions: int = 0,
    ) -> ConstraintResult:
        """
        Run all pre-trade constraint checks.

        Args:
            epic: Instrument epic (e.g. "US100")
            current_spread: Current bid-ask spread
            avg_spread: Average bid-ask spread from data_feed
            has_open_position: Whether this specific epic already has an open position
            total_open_positions: Total number of open positions across all instruments

        Returns ConstraintResult with allowed=True only if ALL checks pass.
        """
        # 1. Kill switch
        if self._state.is_kill_switch_active():
            return ConstraintResult(False, "Kill switch active")

        # 2. Bot not running
        if not self._state.is_running:
            return ConstraintResult(False, "Bot stopped")

        # 3. Weekend check (Mon=0 .. Sun=6)
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return ConstraintResult(False, "Weekend – markets closed")

        # 4. Daily loss limit
        if self._state.is_daily_loss_reached():
            return ConstraintResult(
                False,
                f"Daily loss limit reached ({self._state.daily_pnl:.2f}, "
                f"{self._state.daily_pnl_pct*100:.1f}%)"
            )

        # 5. Position already open for this epic
        if has_open_position:
            return ConstraintResult(False, "Position already open")

        # 6. Max simultaneous positions
        if total_open_positions >= config.MAX_SIMULTANEOUS_POSITIONS:
            return ConstraintResult(
                False,
                f"Max positions reached ({total_open_positions}/{config.MAX_SIMULTANEOUS_POSITIONS})"
            )

        # 7. Available margin check
        margin_check = self._check_available_margin()
        if not margin_check.allowed:
            return margin_check

        # 8. Spread check
        spread_check = self._check_spread(current_spread, avg_spread)
        if not spread_check.allowed:
            return spread_check

        # 9. Session window (not yet open, opening buffer, close buffer, closed)
        close_check = self._check_session_window(epic)
        if not close_check.allowed:
            return close_check

        # 10. News check
        blocked, reason = self._news.get_blocking_info(epic=epic)
        if blocked:
            return ConstraintResult(False, reason)

        return ConstraintResult(True, "")

    def check_all_detailed(
        self,
        epic: str,
        current_spread: float,
        avg_spread: float,
        has_open_position: bool,
        total_open_positions: int = 0,
    ) -> dict[str, dict]:
        """
        Run all pre-trade checks WITHOUT short-circuiting.
        Returns a dict of check_name → {"pass": bool, "reason": str} for every check.
        Used by the dashboard pipeline matrix.
        """
        now = datetime.now(timezone.utc)
        results: dict[str, dict] = {}

        # 1. Kill switch
        ks = not self._state.is_kill_switch_active()
        results["kill_switch"] = {"pass": ks, "reason": "" if ks else "Kill switch active"}

        # 2. Bot running
        br = self._state.is_running
        results["bot_running"] = {"pass": br, "reason": "" if br else "Bot stopped"}

        # 3. Weekend
        wd = now.weekday() < 5
        results["weekend"] = {"pass": wd, "reason": "" if wd else "Weekend"}

        # 4. Daily loss
        dl = not self._state.is_daily_loss_reached()
        results["daily_loss"] = {
            "pass": dl,
            "reason": "" if dl else f"Loss {self._state.daily_pnl_pct*100:.1f}%",
        }

        # 5. Position open
        po = not has_open_position
        results["no_position"] = {"pass": po, "reason": "" if po else "Position open"}

        # 6. Max simultaneous positions
        mp = total_open_positions < config.MAX_SIMULTANEOUS_POSITIONS
        results["max_positions"] = {
            "pass": mp,
            "reason": "" if mp else f"{total_open_positions}/{config.MAX_SIMULTANEOUS_POSITIONS}",
        }

        # 7. Available margin
        mg = self._check_available_margin()
        results["margin"] = {"pass": mg.allowed, "reason": mg.reason}

        # 8. Spread
        sp = self._check_spread(current_spread, avg_spread)
        results["spread"] = {"pass": sp.allowed, "reason": sp.reason}

        # 9. Session window
        cb = self._check_session_window(epic)
        results["session"] = {"pass": cb.allowed, "reason": cb.reason}

        # 10. News
        blocked, reason = self._news.get_blocking_info(epic=epic)
        results["news"] = {"pass": not blocked, "reason": reason}

        return results

    def _check_available_margin(self) -> ConstraintResult:
        """Block if available margin is too low for a new trade."""
        equity = self._state.equity
        available = self._state.available
        if equity <= 0 or available <= 0:
            return ConstraintResult(True, "")  # no data yet, don't block

        avail_pct = available / equity
        if avail_pct < config.MIN_AVAILABLE_PCT:
            return ConstraintResult(
                False,
                f"Low margin: {available:.0f} available "
                f"({avail_pct*100:.0f}% < {config.MIN_AVAILABLE_PCT*100:.0f}%)"
            )
        return ConstraintResult(True, "")

    def _check_spread(self, current_spread: float, avg_spread: float) -> ConstraintResult:
        """Block if spread is abnormally high."""
        if avg_spread <= 0:
            return ConstraintResult(True, "")

        if current_spread > avg_spread * config.SPREAD_THRESHOLD_MULT:
            return ConstraintResult(
                False,
                f"Spread too high: {current_spread:.2f} > "
                f"{avg_spread:.2f} × {config.SPREAD_THRESHOLD_MULT} = "
                f"{avg_spread * config.SPREAD_THRESHOLD_MULT:.2f}"
            )
        return ConstraintResult(True, "")

    def _check_session_window(self, epic: str) -> ConstraintResult:
        """
        Block if outside the trading session window or inside open/close buffers.

        Checks (in order):
        1. Session not yet open today → block
        2. Within opening buffer (first N min after open) → block
        3. Session already closed → block
        4. Within closing buffer (last M min before close) → block
        """
        instrument = self._resolve_instrument(epic)
        if instrument is None:
            return ConstraintResult(True, "")

        session = config.INSTRUMENT_SESSIONS.get(instrument)
        if session is None:
            return ConstraintResult(True, "")

        tz = ZoneInfo(session.timezone)
        now_utc = datetime.now(timezone.utc)
        today_local = now_utc.astimezone(tz).date()

        open_local = datetime.combine(today_local, session.open_time, tzinfo=tz)
        close_local = datetime.combine(today_local, session.close_time, tzinfo=tz)
        open_utc = open_local.astimezone(timezone.utc)
        close_utc = close_local.astimezone(timezone.utc)

        # 1. Session not yet open
        if now_utc < open_utc:
            minutes_until = int((open_utc - now_utc).total_seconds() / 60)
            return ConstraintResult(False, f"Session not yet open ({minutes_until}min to open)")

        # 2. Opening buffer – first N minutes after open are volatile
        minutes_since_open = int((now_utc - open_utc).total_seconds() / 60)
        if minutes_since_open < config.SESSION_NO_NEW_TRADE_OPEN_BUFFER:
            return ConstraintResult(
                False,
                f"Opening buffer: {minutes_since_open}min since open "
                f"(buffer={config.SESSION_NO_NEW_TRADE_OPEN_BUFFER}min)",
            )

        # 3. Session already closed
        remaining = int((close_utc - now_utc).total_seconds() / 60)
        if remaining <= 0:
            return ConstraintResult(False, f"Session closed ({epic})")

        # 4. Closing buffer
        if remaining <= config.SESSION_NO_NEW_TRADE_BUFFER:
            return ConstraintResult(
                False,
                f"Session close buffer: {remaining}min to close "
                f"(buffer={config.SESSION_NO_NEW_TRADE_BUFFER}min)",
            )

        return ConstraintResult(True, "")

    @staticmethod
    def _get_session_close_utc(session: config.SessionWindow) -> datetime:
        """Convert session close time to UTC datetime for today (DST-aware)."""
        tz = ZoneInfo(session.timezone)
        now_utc = datetime.now(timezone.utc)
        today_local = now_utc.astimezone(tz).date()
        close_local = datetime.combine(today_local, session.close_time, tzinfo=tz)
        return close_local.astimezone(timezone.utc)

    @staticmethod
    def _resolve_instrument(epic: str) -> config.Instrument | None:
        """Look up Instrument enum from epic string."""
        for inst in config.Instrument:
            if inst.value == epic:
                return inst
        return None

    @staticmethod
    def minutes_to_session_close(epic: str) -> int | None:
        """Return minutes until session close, or None if unknown/outside."""
        instrument = RiskConstraints._resolve_instrument(epic)
        if instrument is None:
            return None

        session = config.INSTRUMENT_SESSIONS.get(instrument)
        if session is None:
            return None

        now_utc = datetime.now(timezone.utc)
        close_utc = RiskConstraints._get_session_close_utc(session)

        remaining_sec = (close_utc - now_utc).total_seconds()
        return int(remaining_sec / 60)  # negative when session already closed

"""
Orchestrator – event loop and coordination.

Central module: wires all standalone modules together.  Contains NO
business logic – only event routing and lifecycle management.

Flow per 1min candle (per instrument, independently):
  Schicht 4 (risk_constraints) → Schicht 1 (bias, from 15min) →
  EV gate (StatisticsEngine) →
  Schicht 2 (signal_engine, 1min) → Schicht 3 (trade_validator) →
  Order Execution

Exit management (no breakeven – data proved it kills winners):
  1. Trailing stop at +1.0R → activate with wide distance
  2. Time exit: data-driven dead zone and hard max
  3. EOD close: 5min before session end

Multi-asset: each instrument has its own bias, position, and exit
state.  Global state (balance, daily PnL, kill switch) is shared.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from core.capital_client import CapitalClient, Position
from core.data_feed import DataFeed, OHLCVCandle
from core.feature_engine import FeatureEngine
from core.news_filter import NewsFilter, EPIC_CURRENCIES
from core.statistics import StatisticsEngine
from pipeline.regime_classifier import classify_bias, MarketBias
from pipeline.setup_engine import (
    detect_setup, scan_all_conditions, SetupType, Direction,
)
from pipeline.trade_validator import validate_trade
from pipeline.risk_constraints import RiskConstraints
from execution.risk_manager import calculate_position_size
from execution.order_executor import OrderExecutor
from execution.trade_tracker import TradeTracker
from execution.state_manager import StateManager
import config

logger = logging.getLogger(__name__)


@dataclass
class PositionMeta:
    """Per-instrument open position metadata."""
    position: Position
    setup_type: str = ""
    direction: str = ""
    sl_distance: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trailing_set: bool = False       # True once trailing stop activated
    entry_atr: float = 0.0          # 15min ATR at entry
    # Enriched fields for trade recording
    confidence: float = 0.0
    regime: str = ""
    entry_spread: float = 0.0
    tp_distance: float = 0.0
    volatility: str = ""


class Orchestrator:
    """
    Main event loop for the INTRA trading bot.

    Coordinates all modules without containing business logic itself.
    Supports multiple instruments trading simultaneously.
    """

    def __init__(
        self,
        instruments: list[str] | None = None,
        is_live: bool = False,
        use_ws_15min: bool = True,
    ):
        self._instruments = instruments or []
        self._is_live = is_live
        self._use_ws_15min = use_ws_15min

        # Core modules
        self._client = CapitalClient(mode="live" if is_live else "demo")
        self._feed = DataFeed(self._client, use_ws_15min=use_ws_15min)
        self._executor = OrderExecutor(self._client)
        self._news = NewsFilter()
        self._state = StateManager()
        self._stats = StatisticsEngine(instruments=self._instruments)
        self._tracker = TradeTracker(self._client, self._stats)
        self._constraints = RiskConstraints(self._state, self._news)

        # Per-instrument feature engines
        self._engines: dict[str, FeatureEngine] = {}

        # Per-epic open position tracking
        self._open_positions: dict[str, PositionMeta] = {}

        # Per-epic pipeline status for dashboard matrix
        self._pipeline_status: dict[str, dict] = {}

        # Signal cooldown: {epic: {signal_type: last_fire_time}}
        self._signal_cooldowns: dict[str, dict[str, datetime]] = {}

        # Global cooldown: min seconds between any two trade openings
        self._last_trade_opened_at: datetime | None = None

        # Per-epic consecutive order rejection tracking
        self._order_reject_count: dict[str, int] = {}
        self._order_reject_paused_until: dict[str, datetime] = {}

        # Dashboard bridge (set by dashboard_api)
        self.on_status_update: Any = None

        # Shutdown flag
        self._shutdown = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self):
        """Full startup sequence."""
        logger.info("=" * 60)
        logger.info("INTRA Trading Bot starting...")
        logger.info("Mode: %s", "LIVE" if self._is_live else "DEMO")
        logger.info("Instruments: %s", self._instruments)
        logger.info("=" * 60)

        # 1. Login
        account = await self._client.login()
        logger.info("Account: %s, Balance: %.2f %s",
                     account.account_id, account.balance, account.currency)

        # 2. Initialize state
        self._state.initialize(account.balance, self._is_live)
        self._state.set_instruments(self._instruments)
        self._state.set_running(True)

        # 3. Verify instruments exist on Capital.com
        verified = []
        for epic in self._instruments:
            try:
                details = await self._client.get_market_details(epic)
                if details:
                    name = details.get("instrument", {}).get("name", epic)
                    logger.info("Verified epic: %s (%s)", epic, name)
                    verified.append(epic)
                else:
                    logger.warning("Epic not found or unavailable: %s (skipping)", epic)
            except Exception as e:
                logger.warning("Failed to verify epic %s: %s (skipping)", epic, e)
        self._instruments = verified
        if not self._instruments:
            raise RuntimeError("No valid instruments after verification")
        self._state.set_instruments(self._instruments)

        # 4. Check existing positions
        positions = await self._client.get_positions()
        for pos in positions:
            if pos.epic in self._instruments:
                sl_dist = abs(pos.open_level - pos.stop_level) if pos.stop_level else 0.0
                self._open_positions[pos.epic] = PositionMeta(
                    position=pos,
                    setup_type="noise_breakout",
                    direction=pos.direction,
                    sl_distance=sl_dist,
                )
                logger.info(
                    "Found existing position: %s %s size=%.2f SL=%.5f",
                    pos.epic, pos.direction, pos.size, pos.stop_level or 0,
                )

        # 5. Initialize trade tracker (loads history + bootstraps stats)
        await self._tracker.initialize()

        # 5b. Restore daily P&L and trade count from persisted trades
        daily_pnl, daily_count = self._tracker.get_todays_summary()
        self._state.restore_daily(daily_pnl, daily_count)

        # 6. Initialize news filter
        await self._news.initialize()

        # 7. Initialize data feeds (loads historical candles)
        await self._feed.initialize(self._instruments)

        # 8. Initialize feature engines from loaded buffers
        for epic in self._instruments:
            engine = FeatureEngine(epic)
            buf_1min = self._feed.get_1min_buffer(epic)
            buf_15min = self._feed.get_15min_buffer(epic)
            buf_daily = self._feed.get_daily_buffer(epic)
            if buf_1min and buf_15min:
                engine.initialize(
                    buf_1min.get_all(),
                    buf_15min.get_all(),
                    buf_daily.get_all() if buf_daily else None,
                )
            self._engines[epic] = engine

        # 9. Classify initial bias from historical data (per-epic)
        for epic in self._instruments:
            engine = self._engines.get(epic)
            if engine:
                snap = engine.get_15min_snapshot()

                diag_keys = ["ready", "adx", "ema_9", "ema_21", "atr", "atr_avg"]
                diag = {k: snap.get(k) for k in diag_keys}
                none_keys = [k for k in diag_keys if snap.get(k) is None and k != "ready"]
                logger.info(
                    "Snapshot for %s: %s%s",
                    epic, diag,
                    f" — MISSING: {none_keys}" if none_keys else " — all indicators OK",
                )

                result = classify_bias(snap)
                self._state.set_bias(epic, result)
                logger.info(
                    "Initial bias for %s: %s (vol=%s, TQ=%.2f) – %s",
                    epic, result.bias.value, result.volatility,
                    result.trend_quality, result.details,
                )

        # 10. Initialize pipeline matrix with startup data
        for epic in self._instruments:
            bias = self._state.get_bias(epic)
            spread = self._feed.get_avg_spread(epic) or 0.0
            self._pipeline_status[epic] = {
                "constraints": self._constraints.check_all_detailed(
                    epic=epic,
                    current_spread=spread,
                    avg_spread=spread,
                    has_open_position=(epic in self._open_positions),
                    total_open_positions=len(self._open_positions),
                ),
                "bias": bias.bias.value,
                "bias_ok": bias.bias != MarketBias.BLOCKED,
                "volatility": bias.volatility,
                "setup": None,
                "setup_details": [],
                "ev": None,
                "ev_ok": False,
                "blocked_at": "setup" if bias.bias != MarketBias.BLOCKED else "bias",
            }

        # 11. Set callbacks
        self._feed.on_1min_candle = self._on_1min_candle
        self._feed.on_15min_candle = self._on_15min_candle

        # 12. Start background tasks
        tasks = [
            asyncio.create_task(self._poll_balance()),
            asyncio.create_task(self._poll_positions()),
            asyncio.create_task(self._feed.start_streaming(self._instruments)),
        ]

        logger.info("Bot is now running. Waiting for candle events...")

        # Broadcast initial status to dashboard
        self._broadcast_status()

        # Wait for shutdown or task failure
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                if task.exception():
                    logger.error("Task failed: %s", task.exception())
        except asyncio.CancelledError:
            logger.info("Bot shutting down...")
        finally:
            for task in tasks:
                task.cancel()
            await self._shutdown_gracefully()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_15min_candle(self, epic: str, candle: OHLCVCandle):
        """Handle new 15min candle – update bias (per-epic)."""
        engine = self._engines.get(epic)
        if not engine:
            return

        engine.update_15min(candle)
        snapshot = engine.get_15min_snapshot()

        result = classify_bias(snapshot)
        self._state.set_bias(epic, result)
        logger.info(
            "[15min] %s Bias: %s (vol=%s, ADX=%.1f, TQ=%.2f, VP=%s) – %s",
            epic, result.bias.value, result.volatility, result.adx,
            result.trend_quality, result.volume_pressure, result.details,
        )
        self._broadcast_status()

    async def _on_1min_candle(self, epic: str, candle: OHLCVCandle):
        """Handle new 1min candle – run full decision pipeline (per-epic)."""
        engine = self._engines.get(epic)
        if not engine:
            return

        # Update indicators
        engine.update_1min(candle)

        # Feed correlation tracker with 1min returns
        snapshot_1min = engine.get_1min_snapshot()
        ret_1min = snapshot_1min.get("return_5", 0.0) or 0.0
        if ret_1min != 0:
            self._stats.update_correlation(epic, ret_1min)

        # Day reset check
        self._state.check_day_reset()

        # Update spread tracking
        if candle.spread > 0:
            spread = candle.spread
            self._feed.update_spread(epic, spread)
        else:
            spread = self._feed.get_avg_spread(epic) or 0.0
        avg_spread = self._feed.get_avg_spread(epic) or spread

        total_open = len(self._open_positions)

        # --- Pipeline status tracking (always runs, for dashboard matrix) ---
        pipeline = {
            "constraints": self._constraints.check_all_detailed(
                epic=epic,
                current_spread=spread,
                avg_spread=avg_spread,
                has_open_position=(epic in self._open_positions),
                total_open_positions=total_open,
            ),
            "bias": None,
            "bias_ok": False,
            "volatility": None,
            "setup": None,
            "setup_details": [],
            "ev": None,
            "ev_ok": False,
            "blocked_at": None,
        }

        # --- SCHICHT 4: Pre-checks ---
        constraint = self._constraints.check_all(
            epic=epic,
            current_spread=spread,
            avg_spread=avg_spread,
            has_open_position=(epic in self._open_positions),
            total_open_positions=total_open,
        )
        if not constraint.allowed:
            logger.debug("[1min] %s BLOCKED: %s", epic, constraint.reason)
            pipeline["blocked_at"] = "constraints"
            self._pipeline_status[epic] = pipeline
            return

        # --- Circuit breaker: consecutive SL hits ---
        if self._state.is_epic_paused(epic):
            logger.debug("[1min] %s BLOCKED: circuit breaker (consecutive SL)", epic)
            pipeline["blocked_at"] = "circuit_breaker"
            self._pipeline_status[epic] = pipeline
            return

        # --- SCHICHT 1: Market Bias (from 15min) ---
        bias_result = self._state.get_bias(epic)
        pipeline["bias"] = bias_result.bias.value
        pipeline["bias_ok"] = bias_result.bias != MarketBias.BLOCKED
        pipeline["volatility"] = bias_result.volatility

        # --- SCHICHT 2: Signal detection (1min) ---
        # Noise Breakout only fires at :00 and :30
        minute = candle.timestamp.minute
        is_noise_check_time = minute in config.NOISE_CHECK_MINUTES

        setup = detect_setup(snapshot_1min, candle.close) if is_noise_check_time else None
        if is_noise_check_time:
            nb_upper = snapshot_1min.get("noise_upper")
            nb_lower = snapshot_1min.get("noise_lower")
            nb_width = snapshot_1min.get("noise_boundary_width")
            logger.info(
                "[Noise Check :%02d] %s price=%.2f zone=[%.2f – %.2f] width=%.2f %s",
                minute, epic, candle.close,
                nb_lower or 0, nb_upper or 0, nb_width or 0,
                f"→ BREAKOUT {setup.setup_type.value} {setup.direction.value}" if setup else "→ INSIDE",
            )

        # Always scan conditions for pipeline matrix
        scan = scan_all_conditions(snapshot_1min, candle.close)
        pipeline["setup_scan"] = scan

        if setup is None:
            pipeline["setup"] = "no_signal"
            pipeline["blocked_at"] = "setup"
            self._pipeline_status[epic] = pipeline
            return

        # Signal cooldown check (per epic + signal type)
        if self._is_signal_on_cooldown(epic, setup.setup_type):
            logger.debug(
                "[1min] %s SKIP: %s on cooldown", epic, setup.setup_type.value
            )
            pipeline["setup"] = "cooldown"
            pipeline["blocked_at"] = "cooldown"
            self._pipeline_status[epic] = pipeline
            return

        # Global cooldown (min time between any two trade openings)
        if self._last_trade_opened_at is not None:
            elapsed = (datetime.now(timezone.utc) - self._last_trade_opened_at).total_seconds()
            if elapsed < config.GLOBAL_COOLDOWN_SECONDS:
                logger.debug(
                    "[1min] %s SKIP: global cooldown (%ds remaining)",
                    epic, int(config.GLOBAL_COOLDOWN_SECONDS - elapsed),
                )
                pipeline["blocked_at"] = "global_cooldown"
                self._pipeline_status[epic] = pipeline
                return

        # --- Correlation-aware position limit ---
        if self._open_positions:
            eff_count = self._stats.effective_position_count(self._open_positions)
            if eff_count >= config.MAX_CORRELATED_EXPOSURE:
                logger.info(
                    "[1min] %s SKIP: correlated exposure limit (%.1f effective positions >= %.1f)",
                    epic, eff_count, config.MAX_CORRELATED_EXPOSURE,
                )
                pipeline["blocked_at"] = "correlation_limit"
                self._pipeline_status[epic] = pipeline
                return

        pipeline["setup"] = {
            "type": setup.setup_type.value,
            "direction": setup.direction.value,
            "confidence": round(setup.confidence, 3),
        }
        pipeline["setup_details"] = setup.details

        logger.info(
            "[1min] %s SETUP: %s %s conf=%.2f bias=%s VP=%s TQ=%.2f | %s",
            epic, setup.setup_type.value, setup.direction.value,
            setup.confidence, bias_result.bias.value,
            bias_result.volume_pressure, bias_result.trend_quality,
            " | ".join(setup.details),
        )

        # --- SCHICHT 3: Trade validation (15min ATR for SL) ---
        atr = engine.atr_15min
        if atr is None or atr == 0:
            logger.debug("[1min] %s SKIP: 15min ATR not available", epic)
            pipeline["blocked_at"] = "ev"
            pipeline["ev"] = {"reject": "15min ATR not available"}
            self._pipeline_status[epic] = pipeline
            return

        return_kurtosis = snapshot_1min.get("return_kurtosis", 3.0) or 3.0

        ev_result = validate_trade(
            setup, atr, candle.close, spread,
            volatility=bias_result.volatility,
            stats=self._stats,
            return_kurtosis=return_kurtosis,
            epic=epic,
            atr_avg=snapshot_1min.get("atr_avg"),
        )

        pipeline["ev"] = {
            "passes": ev_result.passes_filter,
            "reject": ev_result.reject_reason if not ev_result.passes_filter else None,
        }
        pipeline["ev_ok"] = ev_result.passes_filter

        # Track last setup for dashboard (even if rejected)
        winrate = self._tracker.get_winrate(setup.setup_type)
        self._state.set_last_setup(epic, {
            "epic": epic,
            "setup_type": setup.setup_type.value,
            "direction": setup.direction.value,
            "confidence": round(setup.confidence, 3),
            "winrate": round(winrate, 3),
            "spread": round(spread, 4),
            "passed": ev_result.passes_filter,
            "reject_reason": ev_result.reject_reason if not ev_result.passes_filter else None,
        })

        self._pipeline_status[epic] = pipeline
        self._broadcast_status()

        if not ev_result.passes_filter:
            logger.info(
                "[1min] %s TRADE REJECT: %s",
                epic, ev_result.reject_reason,
            )
            pipeline["blocked_at"] = "ev"
            self._pipeline_status[epic] = pipeline
            return

        pipeline["blocked_at"] = None  # All passed!
        self._pipeline_status[epic] = pipeline

        # --- EXECUTE TRADE ---
        await self._execute_trade(
            epic, setup, ev_result, atr,
            bias_result=bias_result,
            spread=spread,
        )

    # ------------------------------------------------------------------
    # Signal cooldown
    # ------------------------------------------------------------------

    def _is_signal_on_cooldown(self, epic: str, setup_type: SetupType) -> bool:
        epic_cooldowns = self._signal_cooldowns.get(epic, {})
        last_fire = epic_cooldowns.get(setup_type.value)
        if last_fire is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last_fire).total_seconds() / 60
        return elapsed < config.SIGNAL_COOLDOWN_MINUTES

    def _record_signal_cooldown(self, epic: str, setup_type: SetupType):
        if epic not in self._signal_cooldowns:
            self._signal_cooldowns[epic] = {}
        self._signal_cooldowns[epic][setup_type.value] = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _execute_trade(
        self, epic, setup, ev_result, atr,
        bias_result=None, spread=0.0,
    ):
        """Open a position for a specific instrument."""
        # Order rejection cooldown
        paused_until = self._order_reject_paused_until.get(epic)
        if paused_until and datetime.now(timezone.utc) < paused_until:
            logger.debug("[%s] Order rejection cooldown active – skipping", epic)
            return

        pos_size = calculate_position_size(
            self._state.balance,
            ev_result.sl_distance,
            current_price=ev_result.entry_price,
            max_leverage=config.INSTRUMENT_MAX_LEVERAGE.get(epic),
            stats=self._stats,
            setup_type=setup.setup_type.value,
            open_positions=self._open_positions,
        )

        if pos_size.skip:
            logger.info(
                "[%s] Trade skipped: effective risk %.2f EUR too small after leverage cap",
                epic, pos_size.effective_risk,
            )
            return

        # Check Capital.com minimum deal size
        instrument_min = config.INSTRUMENT_MIN_SIZE.get(epic, 0.01)
        if pos_size.size < instrument_min:
            logger.info(
                "[%s] Trade skipped: size=%.2f below instrument minimum=%.2f",
                epic, pos_size.size, instrument_min,
            )
            return

        order = await self._executor.open_trade(
            epic=epic,
            direction=setup.direction.value,
            size=pos_size.size,
            sl_price=ev_result.sl_price,
            tp_price=ev_result.tp_price,
        )

        if order.success:
            self._open_positions[epic] = PositionMeta(
                position=Position(
                    deal_id=order.deal_id,
                    epic=epic,
                    direction=setup.direction.value,
                    size=pos_size.size,
                    open_level=order.level,
                    current_level=order.level,
                    stop_level=ev_result.sl_price,
                    profit_level=ev_result.tp_price,
                ),
                setup_type=setup.setup_type.value,
                direction=setup.direction.value,
                sl_distance=ev_result.sl_distance,
                entry_atr=atr,
                # Enriched fields
                confidence=setup.confidence,
                regime=bias_result.bias.value if bias_result else "",
                entry_spread=spread,
                tp_distance=ev_result.tp_distance,
                volatility=bias_result.volatility if bias_result else "",
            )

            # Record cooldowns
            self._record_signal_cooldown(epic, setup.setup_type)
            self._last_trade_opened_at = datetime.now(timezone.utc)

            is_lowprice = ev_result.entry_price < 200
            sl_fmt = f"{ev_result.sl_price:.5f}" if is_lowprice else f"{ev_result.sl_price:.2f}"
            tp_fmt = f"{ev_result.tp_price:.5f}" if is_lowprice else f"{ev_result.tp_price:.2f}"
            winrate = self._tracker.get_winrate(setup.setup_type)
            ev = self._stats.get_ev(setup.setup_type.value)
            kelly = self._stats.get_kelly_fraction(setup.setup_type.value)
            logger.info(
                "TRADE OPENED: %s %s size=%.2f SL=%s TP=%s RRR=%.2f "
                "conf=%.2f WR=%.2f EV=%.2f Kelly=%.3f",
                epic, setup.direction.value, pos_size.size,
                sl_fmt, tp_fmt, ev_result.rrr,
                setup.confidence, winrate, ev, kelly,
            )
            self._order_reject_count[epic] = 0
            self._broadcast_status()
        else:
            logger.warning("TRADE FAILED: %s – %s", epic, order.message)
            count = self._order_reject_count.get(epic, 0) + 1
            self._order_reject_count[epic] = count
            if count >= config.ORDER_REJECT_MAX:
                pause_until = datetime.now(timezone.utc) + timedelta(
                    minutes=config.ORDER_REJECT_PAUSE_MINUTES
                )
                self._order_reject_paused_until[epic] = pause_until
                self._order_reject_count[epic] = 0
                logger.warning(
                    "[%s] %d consecutive order rejections – pausing for %d min",
                    epic, count, config.ORDER_REJECT_PAUSE_MINUTES,
                )

    # ------------------------------------------------------------------
    # Polling tasks
    # ------------------------------------------------------------------

    async def _poll_balance(self):
        """Poll account balance every N seconds + periodic news refresh."""
        poll_count = 0
        while not self._shutdown:
            try:
                accounts = await self._client.get_accounts()
                if accounts:
                    acc = accounts[0]
                    self._state.update_balance(
                        acc.balance,
                        equity=acc.balance + acc.profit_loss,
                        margin_used=acc.deposit,
                        available=acc.available,
                    )
                    self._broadcast_status()
            except Exception as e:
                logger.warning("Balance poll error: %s", e)

            # Periodic news refresh
            poll_count += 1
            if poll_count % 360 == 0:
                try:
                    await self._news.refresh_if_needed()
                except Exception as e:
                    logger.warning("News refresh error: %s", e)

            await asyncio.sleep(config.POLL_BALANCE_INTERVAL)

    async def _poll_positions(self):
        """Poll positions, run exit management, reconcile closures."""
        while not self._shutdown:
            try:
                api_positions = await self._client.get_positions()

                # Build epic -> Position map from API
                api_by_epic: dict[str, Position] = {}
                for p in api_positions:
                    api_by_epic[p.epic] = p

                # Detect closed positions
                closed_epics = [
                    epic for epic in list(self._open_positions.keys())
                    if epic not in api_by_epic
                ]
                for epic in closed_epics:
                    await self._handle_position_closed(epic)

                # Update live positions + exit management + EOD
                for epic, meta in list(self._open_positions.items()):
                    if epic in api_by_epic:
                        meta.position = api_by_epic[epic]
                        await self._manage_exit(epic)
                        await self._check_eod_close(epic)

            except Exception as e:
                logger.warning("Position poll error: %s", e)
            await asyncio.sleep(config.POLL_POSITIONS_INTERVAL)

    # ------------------------------------------------------------------
    # Exit management (NO breakeven – trailing stop + time exits)
    # ------------------------------------------------------------------

    async def _manage_exit(self, epic: str):
        """
        Exit management called every position poll cycle.

        Only trailing stop activation. No time-based exits.
        Positions exit via: SL, trailing stop, or EOD close (paper-conformant).
        """
        meta = self._open_positions.get(epic)
        if meta is None:
            return

        pos = meta.position
        pnl = pos.profit_loss

        # Calculate 1R reference
        if meta.sl_distance > 0 and pos.size > 0:
            one_r = meta.sl_distance * pos.size
        else:
            one_r = max(abs(pnl), 10.0)

        r_multiple = pnl / one_r if one_r > 0 else 0.0

        # --- Trailing stop at +1.0R ---
        if not meta.trailing_set and r_multiple >= config.TRAILING_ACTIVATE_R:
            try:
                trail_distance = config.TRAILING_MIN_ATR_MULT * meta.entry_atr
                stats_trail = self._stats.get_optimal_trail_atr_mult(
                    meta.setup_type
                ) * meta.entry_atr
                trail_distance = max(trail_distance, stats_trail)

                if trail_distance > 0:
                    await self._client.modify_position(
                        pos.deal_id,
                        trailing_stop=True,
                        trailing_stop_distance=trail_distance,
                        profit_level=pos.profit_level,
                    )
                    meta.trailing_set = True
                    logger.info(
                        "TRAILING STOP for %s: distance=%.2f (%.1f*ATR) "
                        "pnl=%.2f (%.1fR)",
                        epic, trail_distance,
                        trail_distance / meta.entry_atr if meta.entry_atr > 0 else 0,
                        pnl, r_multiple,
                    )
            except Exception as e:
                logger.warning("Failed to set trailing stop for %s: %s", epic, e)

        held_minutes = (datetime.now(timezone.utc) - meta.opened_at).total_seconds() / 60
        logger.debug(
            "EXIT HOLD [%s]: pnl=%.2f (%.1fR) held=%.0fmin TS=%s",
            epic, pnl, r_multiple, held_minutes, meta.trailing_set,
        )

    async def _check_eod_close(self, epic: str):
        """Close position before session end (intraday – no overnight)."""
        meta = self._open_positions.get(epic)
        if meta is None:
            return

        remaining = RiskConstraints.minutes_to_session_close(epic)
        if remaining is None:
            return

        if remaining <= config.SESSION_FORCE_CLOSE_BUFFER:
            logger.warning(
                "EOD CLOSE: %dmin to session close for %s, closing position %s",
                remaining, epic, meta.position.deal_id,
            )
            try:
                closed = await self._executor.close_trade(meta.position.deal_id)
                if closed:
                    logger.info("EOD position closed for %s", epic)
            except Exception as e:
                logger.error("Failed to EOD-close position for %s: %s", epic, e)

    async def _handle_position_closed(self, epic: str):
        """Handle a position that was closed (SL/TP/trailing hit)."""
        meta = self._open_positions.get(epic)
        if meta is None:
            return

        logger.info("Position closed detected for %s", epic)

        try:
            # Capital.com can take a few seconds to register the transaction
            await asyncio.sleep(5)

            from_date = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            transactions = await self._client.get_transactions(from_date=from_date)

            exit_price = meta.position.current_level or meta.position.open_level
            entry_price = meta.position.open_level or 0.0
            pnl = 0.0

            if transactions:
                sorted_txns = sorted(transactions, key=lambda t: t.date, reverse=True)

                for i, t in enumerate(sorted_txns[:5]):
                    logger.info(
                        "Transaction[%d]: type=%s deal_id=%s instrument=%s pnl=%.2f date=%s",
                        i, t.transaction_type, t.deal_id, t.instrument_name,
                        t.profit_loss, t.date,
                    )

                match = None
                deal_id = meta.position.deal_id

                for t in sorted_txns:
                    if t.deal_id and t.deal_id == deal_id:
                        match = t
                        break

                if match is None:
                    for t in sorted_txns:
                        epic_match = (
                            t.epic == epic
                            or epic in t.instrument_name
                        )
                        if epic_match:
                            match = t
                            break

                if match is not None:
                    pnl = match.profit_loss
                    exit_price = match.close_level or exit_price
                    logger.info(
                        "PnL from transaction API for %s: %.2f (type=%s, ref=%s)",
                        epic, pnl, match.transaction_type, match.reference,
                    )
                else:
                    logger.warning(
                        "No matching transaction found for %s (deal_id=%s) "
                        "– %d transactions checked, using P&L=0",
                        epic, deal_id, len(sorted_txns),
                    )

            # --- Enriched trade recording ---
            if meta.setup_type and meta.direction:
                try:
                    st = SetupType(meta.setup_type)
                    dr = Direction(meta.direction)

                    hold_minutes = (
                        datetime.now(timezone.utc) - meta.opened_at
                    ).total_seconds() / 60

                    exit_reason = self._determine_exit_reason(meta, pnl)

                    self._tracker.record_trade(
                        setup_type=st,
                        direction=dr,
                        epic=epic,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl=pnl,
                        hold_minutes=hold_minutes,
                        exit_reason=exit_reason,
                        regime=meta.regime,
                        confidence=meta.confidence,
                        atr_at_entry=meta.entry_atr,
                        spread_at_entry=meta.entry_spread,
                        sl_distance=meta.sl_distance,
                        tp_distance=meta.tp_distance,
                        volatility=meta.volatility,
                    )
                except (ValueError, KeyError) as e:
                    logger.warning("Failed to record trade for %s: %s", epic, e)

            # Record in state manager (per-epic circuit breaker + global)
            self._state.record_trade(pnl, epic=epic, sl_distance=meta.sl_distance)

            logger.info(
                "Trade completed for %s: PnL=%.2f (%s) held=%.0fmin exit=%s",
                epic, pnl, "WIN" if pnl > 0 else "LOSS",
                (datetime.now(timezone.utc) - meta.opened_at).total_seconds() / 60,
                self._determine_exit_reason(meta, pnl),
            )
        except Exception as e:
            logger.error("Failed to process closed position for %s: %s", epic, e)

        # Remove from tracking
        del self._open_positions[epic]
        self._broadcast_status()

    def _determine_exit_reason(self, meta: PositionMeta, pnl: float) -> str:
        """Infer exit reason from position metadata and P&L."""
        pos = meta.position
        held_minutes = (datetime.now(timezone.utc) - meta.opened_at).total_seconds() / 60

        # Check EOD (session close buffer)
        remaining = RiskConstraints.minutes_to_session_close(pos.epic)
        if remaining is not None and remaining <= config.SESSION_FORCE_CLOSE_BUFFER + 2:
            return "eod"

        # Check time exits
        optimal_hold = self._stats.get_optimal_hold_minutes(meta.setup_type)
        dead_threshold = max(optimal_hold * 2, config.TIME_EXIT_DEAD_MINUTES_FLOOR)
        hard_max = max(optimal_hold * 3, config.TIME_EXIT_MAX_MINUTES_FLOOR)

        if meta.sl_distance > 0 and pos.size > 0:
            one_r = meta.sl_distance * pos.size
            r_multiple = pnl / one_r if one_r > 0 else 0
        else:
            r_multiple = 0

        if held_minutes >= hard_max:
            return "time_max"
        if held_minutes >= dead_threshold and abs(r_multiple) < config.TIME_EXIT_DEAD_R:
            return "time_dead"

        # Check trailing stop
        if meta.trailing_set:
            return "trailing"

        # Check TP hit (profit near or above TP distance)
        if pnl > 0 and meta.tp_distance > 0:
            price_move = abs((pos.current_level or 0) - (pos.open_level or 0))
            if price_move >= meta.tp_distance * 0.9:
                return "tp"

        # Default: SL hit (loss or small profit without trailing)
        if pnl <= 0:
            return "sl"

        return "unknown"

    # ------------------------------------------------------------------
    # Dashboard bridge
    # ------------------------------------------------------------------

    def _broadcast_status(self):
        if self.on_status_update:
            try:
                self.on_status_update(self.get_full_status())
            except Exception:
                pass

    def get_full_status(self) -> dict:
        """Build complete status dict for dashboard."""
        status = self._state.get_status()
        status["winrate_stats"] = self._tracker.get_all_stats()
        # Per-epic win rates
        epic_wr = {}
        for epic in self._instruments:
            epic_wr[epic] = self._tracker.get_all_stats(epic=epic)
        status["winrate_stats_by_epic"] = epic_wr
        status["recent_trades"] = self._tracker.get_recent_trades(100)

        # Multi-position: dict of epic -> position info
        positions_dict = {}
        for epic, meta in self._open_positions.items():
            positions_dict[epic] = {
                "deal_id": meta.position.deal_id,
                "epic": meta.position.epic,
                "direction": meta.position.direction,
                "size": meta.position.size,
                "open_level": meta.position.open_level,
                "current_level": meta.position.current_level,
                "stop_level": meta.position.stop_level,
                "profit_level": meta.position.profit_level,
                "trailing_stop": meta.position.trailing_stop,
                "trailing_stop_distance": meta.position.trailing_stop_distance,
                "pnl": meta.position.profit_loss,
                "setup_type": meta.setup_type,
                "trailing_set": meta.trailing_set,
                "confidence": meta.confidence,
                "held_minutes": int((datetime.now(timezone.utc) - meta.opened_at).total_seconds() / 60),
            }
        status["open_positions"] = positions_dict

        if positions_dict:
            first_key = next(iter(positions_dict))
            status["open_position"] = positions_dict[first_key]
        else:
            status["open_position"] = None

        status["news_events"] = self._news.get_upcoming_events()
        status["epic_currencies"] = EPIC_CURRENCIES
        status["pipeline_matrix"] = dict(self._pipeline_status)

        return status

    # ------------------------------------------------------------------
    # Control methods (called from dashboard / CLI)
    # ------------------------------------------------------------------

    async def stop(self):
        self._state.set_running(False)
        self._shutdown = True
        logger.info("Bot stop requested")

    async def kill_switch(self):
        self._state.activate_kill_switch()
        closed = await self._executor.close_all_positions()
        self._open_positions.clear()
        self._state.set_running(False)
        self._shutdown = True
        logger.warning("KILL SWITCH: %d positions closed, bot stopped", closed)

    async def _shutdown_gracefully(self):
        await self._feed.stop_streaming()
        await self._client.close()
        # Persist stats on shutdown
        self._stats.save()
        logger.info("Bot shut down cleanly")

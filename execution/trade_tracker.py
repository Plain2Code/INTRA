"""
Trade tracker – enriched trade outcome tracking with StatisticsEngine.

Tracks wins/losses per setup type with rich contextual data:
hold time, exit reason, regime, confidence, ATR, spread, volatility.

Delegates statistical computation to StatisticsEngine.
Persists to trades.json for survival across restarts.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

import config
from core.capital_client import CapitalClient
from core.statistics import StatisticsEngine
from pipeline.setup_engine import SetupType, Direction

logger = logging.getLogger(__name__)


@dataclass
class CompletedTrade:
    """Enriched trade record with full context for statistical analysis."""
    epic: str
    direction: str
    setup_type: str
    entry_price: float
    exit_price: float
    pnl: float
    is_win: bool
    timestamp: str = ""
    # Enriched fields (default to 0/"" for backward compat with old trades.json)
    hold_minutes: float = 0.0
    exit_reason: str = ""        # "sl", "tp", "trailing", "time_dead", "time_max", "eod"
    regime: str = ""             # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: float = 0.0      # signal confidence [0,1]
    atr_at_entry: float = 0.0
    spread_at_entry: float = 0.0
    sl_distance: float = 0.0
    tp_distance: float = 0.0
    volatility: str = ""         # "low", "normal", "high", "extreme"


class TradeTracker:
    """
    Tracks trade outcomes and delegates stats to StatisticsEngine.

    Usage:
        tracker = TradeTracker(client, stats_engine)
        await tracker.initialize()
        tracker.record_trade(...)
        wr = tracker.get_winrate(SetupType.NOISE_BREAKOUT)
    """

    def __init__(self, client: CapitalClient, stats: StatisticsEngine):
        self._client = client
        self._stats = stats
        self._recent_trades: list[CompletedTrade] = []

    async def initialize(self):
        """Load persisted trade history and bootstrap stats engine."""
        self._load()

        try:
            from_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
            transactions = await self._client.get_transactions(from_date=from_date)
            trade_count = sum(
                1 for t in transactions
                if t.transaction_type in ("TRADE", "ORDER")
            )
            logger.debug("Capital.com: %d trades (7d), local: %d trades", trade_count, len(self._recent_trades))
        except Exception as e:
            logger.warning("Failed to load Capital.com trade history: %s", e)

    def record_trade(
        self,
        setup_type: SetupType,
        direction: Direction,
        epic: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        hold_minutes: float = 0.0,
        exit_reason: str = "",
        regime: str = "",
        confidence: float = 0.0,
        atr_at_entry: float = 0.0,
        spread_at_entry: float = 0.0,
        sl_distance: float = 0.0,
        tp_distance: float = 0.0,
        volatility: str = "",
    ):
        """Record a completed trade with full context."""
        is_win = pnl > 0

        trade = CompletedTrade(
            epic=epic,
            direction=direction.value,
            setup_type=setup_type.value,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            is_win=is_win,
            timestamp=datetime.now(timezone.utc).isoformat(),
            hold_minutes=hold_minutes,
            exit_reason=exit_reason,
            regime=regime,
            confidence=confidence,
            atr_at_entry=atr_at_entry,
            spread_at_entry=spread_at_entry,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            volatility=volatility,
        )
        self._recent_trades.append(trade)

        # Feed statistics engine
        self._stats.record_trade(
            setup_type=setup_type.value,
            epic=epic,
            pnl=pnl,
            hold_minutes=hold_minutes,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
        )

        self._save()
        self._stats.save()

        logger.debug("Trade saved: %s %s PnL=%.2f", epic, setup_type.value, pnl)

    def get_winrate(self, setup_type: SetupType) -> float:
        """Get current win rate for a setup type."""
        return self._stats.get_winrate(setup_type.value)

    def get_all_stats(self, epic: str | None = None) -> dict[str, Any]:
        """Get stats for dashboard."""
        if epic is None:
            return self._stats.get_all_stats_summary()
        return self._stats.get_epic_stats_summary(epic)

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        """Get recent trades for dashboard."""
        trades = self._recent_trades[-limit:]
        return [
            {
                "epic": t.epic,
                "direction": t.direction,
                "setup_type": t.setup_type,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "is_win": t.is_win,
                "timestamp": t.timestamp,
                "hold_minutes": t.hold_minutes,
                "exit_reason": t.exit_reason,
                "regime": t.regime,
                "confidence": t.confidence,
            }
            for t in reversed(trades)
        ]

    def get_todays_summary(self) -> tuple[float, int]:
        """Return (total_pnl, trade_count) for today's trades."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_pnl = 0.0
        count = 0
        for t in self._recent_trades:
            if t.timestamp and t.timestamp[:10] == today:
                total_pnl += t.pnl
                count += 1
        return total_pnl, count

    # ------------------------------------------------------------------
    # Persistence (backward compatible)
    # ------------------------------------------------------------------

    def _save(self):
        """Write trade list to trades.json."""
        data = {
            "trades": [
                {
                    "epic": t.epic,
                    "direction": t.direction,
                    "setup_type": t.setup_type,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": t.pnl,
                    "is_win": t.is_win,
                    "timestamp": t.timestamp,
                    "hold_minutes": t.hold_minutes,
                    "exit_reason": t.exit_reason,
                    "regime": t.regime,
                    "confidence": t.confidence,
                    "atr_at_entry": t.atr_at_entry,
                    "spread_at_entry": t.spread_at_entry,
                    "sl_distance": t.sl_distance,
                    "tp_distance": t.tp_distance,
                    "volatility": t.volatility,
                }
                for t in self._recent_trades
            ],
        }
        try:
            with open(config.TRADES_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save trades.json: %s", e)

    def _load(self):
        """Load trade list from trades.json and bootstrap stats engine."""
        if not os.path.exists(config.TRADES_FILE):
            return
        try:
            with open(config.TRADES_FILE) as f:
                data = json.load(f)

            for t in data.get("trades", []):
                trade = CompletedTrade(
                    epic=t["epic"],
                    direction=t["direction"],
                    setup_type=t["setup_type"],
                    entry_price=t["entry_price"],
                    exit_price=t["exit_price"],
                    pnl=t["pnl"],
                    is_win=t["is_win"],
                    timestamp=t.get("timestamp", ""),
                    hold_minutes=t.get("hold_minutes", 0.0),
                    exit_reason=t.get("exit_reason", ""),
                    regime=t.get("regime", ""),
                    confidence=t.get("confidence", 0.0),
                    atr_at_entry=t.get("atr_at_entry", 0.0),
                    spread_at_entry=t.get("spread_at_entry", 0.0),
                    sl_distance=t.get("sl_distance", 0.0),
                    tp_distance=t.get("tp_distance", 0.0),
                    volatility=t.get("volatility", ""),
                )
                self._recent_trades.append(trade)

                # Bootstrap stats engine from historical trades
                self._stats.record_trade(
                    setup_type=trade.setup_type,
                    epic=trade.epic,
                    pnl=trade.pnl,
                    hold_minutes=trade.hold_minutes,
                    sl_distance=trade.sl_distance,
                    tp_distance=trade.tp_distance,
                )

            logger.debug("Loaded %d trades from trades.json", len(self._recent_trades))

            # Bootstrap is the single source of truth (trades.json has all trades)
            # Always save after bootstrap to keep stats.json in sync
            self._stats.save()

        except (OSError, json.JSONDecodeError, KeyError, AttributeError) as e:
            logger.warning("Failed to load trades.json (starting fresh): %s", e)

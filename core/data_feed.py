"""
Data feed module – candle buffers and stream management.

Standalone module: manages rolling OHLCV buffers per instrument per
timeframe.  Loads historical candles on startup via REST, then receives
live updates.  Fires callbacks when candles close.

15min feed method: currently supports both WebSocket and REST-polling
fallback.  Set via constructor parameter.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.capital_client import CapitalClient, Candle
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candle data class (internal representation)
# ---------------------------------------------------------------------------

@dataclass
class OHLCVCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float = 0.0  # bid-ask spread

    @classmethod
    def from_api_candle(cls, c: Candle) -> OHLCVCandle:
        ts = _parse_timestamp(c.timestamp)
        return cls(
            timestamp=ts,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
            spread=c.spread,
        )


def _parse_timestamp(ts_value) -> datetime:
    """Parse various Capital.com timestamp formats (string or epoch ms)."""
    # Unix epoch in milliseconds (integer or numeric string)
    if isinstance(ts_value, (int, float)):
        return datetime.fromtimestamp(ts_value / 1000, tz=timezone.utc)
    if isinstance(ts_value, str) and ts_value.isdigit():
        return datetime.fromtimestamp(int(ts_value) / 1000, tz=timezone.utc)

    if isinstance(ts_value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(ts_value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Rolling candle buffer
# ---------------------------------------------------------------------------

class CandleBuffer:
    """Fixed-size rolling buffer for OHLCV candles."""

    def __init__(self, maxlen: int = 500):
        self.maxlen = maxlen
        self._candles: deque[OHLCVCandle] = deque(maxlen=maxlen)

    def append(self, candle: OHLCVCandle):
        self._candles.append(candle)

    @property
    def last(self) -> OHLCVCandle | None:
        return self._candles[-1] if self._candles else None

    def get_last_n(self, n: int) -> list[OHLCVCandle]:
        """Return last n candles (oldest first)."""
        candles = list(self._candles)
        return candles[-n:] if n < len(candles) else candles

    def get_all(self) -> list[OHLCVCandle]:
        return list(self._candles)

    def __len__(self) -> int:
        return len(self._candles)

    def __getitem__(self, idx: int) -> OHLCVCandle:
        return self._candles[idx]


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

CandleCallback = Callable[[str, OHLCVCandle], Coroutine[Any, Any, None]]
# (epic, candle) -> awaitable


# ---------------------------------------------------------------------------
# Data Feed Manager
# ---------------------------------------------------------------------------

class DataFeed:
    """
    Manages candle buffers and live data streams for multiple instruments.

    Usage:
        feed = DataFeed(client)
        feed.on_1min_candle = my_1min_handler
        feed.on_15min_candle = my_15min_handler
        await feed.initialize(["US100", "DE40"])
        await feed.start_streaming(["US100", "DE40"])
    """

    def __init__(
        self,
        client: CapitalClient,
        use_ws_15min: bool = True,
        poll_15min_interval: float = 60.0,
    ):
        self._client = client
        self._use_ws_15min = use_ws_15min
        self._poll_15min_interval = poll_15min_interval

        # Buffers: epic -> buffer
        self._buffers_1min: dict[str, CandleBuffer] = {}
        self._buffers_15min: dict[str, CandleBuffer] = {}
        self._buffers_daily: dict[str, CandleBuffer] = {}

        # Last known candle timestamps to detect new candles
        self._last_1min_ts: dict[str, str] = {}
        self._last_15min_ts: dict[str, str] = {}

        # Callbacks
        self.on_1min_candle: CandleCallback | None = None
        self.on_15min_candle: CandleCallback | None = None

        # For 15min polling fallback
        self._poll_task: asyncio.Task | None = None
        self._active_epics: list[str] = []

        # Spread tracking: average bid-ask spread per epic
        self._spread_sums: dict[str, float] = {}
        self._spread_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Initialization: load historical candles
    # ------------------------------------------------------------------

    async def initialize(self, epics: list[str]):
        """Load historical candles for all instruments."""
        self._active_epics = epics

        for epic in epics:
            self._buffers_1min[epic] = CandleBuffer(config.BUFFER_1MIN)
            self._buffers_15min[epic] = CandleBuffer(config.BUFFER_15MIN)
            self._buffers_daily[epic] = CandleBuffer(config.BUFFER_DAILY)

        # Load historical candles sequentially per epic to avoid 429 rate limits
        for epic in epics:
            await self._load_history(epic, "MINUTE", config.BUFFER_1MIN, self._buffers_1min[epic])
            await self._load_history(epic, "MINUTE_15", config.BUFFER_15MIN, self._buffers_15min[epic])
            await self._load_history(epic, "DAY", config.BUFFER_DAILY, self._buffers_daily[epic])
            if epic != epics[-1]:
                await asyncio.sleep(0.2)  # small delay between epics

        for epic in epics:
            logger.info(
                "Buffers loaded for %s: 1min=%d, 15min=%d, daily=%d candles",
                epic, len(self._buffers_1min[epic]),
                len(self._buffers_15min[epic]), len(self._buffers_daily[epic])
            )

    async def _load_history(
        self, epic: str, resolution: str, max_candles: int, buffer: CandleBuffer
    ):
        try:
            candles = await self._client.get_prices(
                epic, resolution=resolution, max_candles=max_candles
            )
            for c in candles:
                ohlcv = OHLCVCandle.from_api_candle(c)
                buffer.append(ohlcv)
                # Track bid-ask spread from historical data (1min only)
                if resolution == "MINUTE" and ohlcv.spread > 0:
                    self._spread_sums[epic] = self._spread_sums.get(epic, 0.0) + ohlcv.spread
                    self._spread_counts[epic] = self._spread_counts.get(epic, 0) + 1
            if buffer.last:
                ts_key = buffer.last.timestamp.isoformat()
                if resolution == "MINUTE":
                    self._last_1min_ts[epic] = ts_key
                else:
                    self._last_15min_ts[epic] = ts_key
            if epic in self._spread_counts and self._spread_counts[epic] > 0:
                avg = self._spread_sums[epic] / self._spread_counts[epic]
                logger.info("Average spread for %s: %.2f (%d samples)",
                            epic, avg, self._spread_counts[epic])
        except Exception as e:
            logger.error("Failed to load %s history for %s: %s", resolution, epic, e)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def start_streaming(self, epics: list[str]):
        """
        Start receiving live candle data with automatic reconnection.
        - 1min: always via WebSocket
        - 15min: via WebSocket if use_ws_15min=True, otherwise REST polling
        """
        resolutions = ["MINUTE"]
        if self._use_ws_15min:
            resolutions.append("MINUTE_15")

        # Start 15min polling fallback if needed (once)
        if not self._use_ws_15min and self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_15min_loop(epics))

        backoff = 2
        max_backoff = 60

        while True:
            try:
                await self._client.ws_connect()
                await self._client.ws_subscribe_ohlc(epics, resolutions)
                backoff = 2  # reset on successful connect
                logger.info("WebSocket streaming active, listening for candles...")
                # Blocks until disconnect
                await self._client.ws_listen(self._handle_ws_message)
            except Exception as e:
                logger.error("WebSocket streaming error: %s", e)

            # Connection dropped — reconnect
            logger.warning("WebSocket disconnected, reconnecting in %ds...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

            # Re-authenticate in case tokens expired
            try:
                await self._client.create_session()
                logger.info("Re-authenticated for WebSocket reconnect")
            except Exception as e:
                logger.error("Re-auth failed: %s, retrying...", e)

    async def stop_streaming(self):
        """Stop all streaming."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        await self._client.ws_disconnect()

    # ------------------------------------------------------------------
    # WebSocket message handler
    # ------------------------------------------------------------------

    async def _handle_ws_message(self, data: dict):
        """Route incoming WebSocket OHLC messages to the right buffer."""
        destination = data.get("destination", "")
        status = data.get("status", "")

        # Skip pings
        if destination == "ping":
            return

        # Skip subscription confirmations (dest=OHLCMarketData.subscribe, status=OK)
        # but NOT actual OHLC events (dest=ohlc.event, status=OK)
        if status == "OK" and "ohlc.event" not in destination.lower():
            return

        if "OHLCMarketData" not in destination and "ohlc" not in destination.lower():
            return

        payload = data.get("payload", data)
        epic = payload.get("epic", "")
        resolution = payload.get("resolution", "")

        if not epic:
            logger.debug("WS OHLC message without epic: %s", str(data)[:300])
            return

        logger.debug("WS OHLC: epic=%s res=%s payload_keys=%s", epic, resolution, list(payload.keys()))

        try:
            # Capital.com WS OHLC fields: t (timestamp), o, h, l, c, v
            ts_raw = payload.get("t", payload.get("snapshotTimeUTC",
                     payload.get("snapshotTime", "")))

            # Extract close price – try bid/ask dict first for spread calc
            close_price_raw = payload.get("closePrice")
            if isinstance(close_price_raw, dict):
                c_bid = float(close_price_raw.get("bid", 0))
                c_ask = float(close_price_raw.get("ask", 0))
                close_val = c_bid
                ws_spread = max(c_ask - c_bid, 0.0) if c_bid > 0 and c_ask > 0 else 0.0
            else:
                close_val = float(payload.get("c", close_price_raw or 0))
                ws_spread = 0.0

            candle = OHLCVCandle(
                timestamp=_parse_timestamp(ts_raw),
                open=float(payload.get("o", payload.get("openPrice", {}).get("bid", 0) if isinstance(payload.get("openPrice"), dict) else payload.get("openPrice", 0))),
                high=float(payload.get("h", payload.get("highPrice", {}).get("bid", 0) if isinstance(payload.get("highPrice"), dict) else payload.get("highPrice", 0))),
                low=float(payload.get("l", payload.get("lowPrice", {}).get("bid", 0) if isinstance(payload.get("lowPrice"), dict) else payload.get("lowPrice", 0))),
                close=close_val,
                volume=float(payload.get("v", payload.get("lastTradedVolume", 0))),
                spread=ws_spread,
            )
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse OHLC message: %s | raw: %s", e, str(payload)[:300])
            return

        ts_key = candle.timestamp.isoformat()

        # Route by resolution: explicit match, then fallback
        res_upper = resolution.upper()
        if res_upper in ("MINUTE_15", "15MIN", "M15"):
            await self._process_candle(
                epic, candle, ts_key,
                self._buffers_15min, self._last_15min_ts,
                self.on_15min_candle,
            )
        elif res_upper in ("MINUTE", "1MIN", "M1", ""):
            await self._process_candle(
                epic, candle, ts_key,
                self._buffers_1min, self._last_1min_ts,
                self.on_1min_candle,
            )
        else:
            logger.debug("Unknown resolution '%s' for epic %s, skipping", resolution, epic)

    async def _process_candle(
        self,
        epic: str,
        candle: OHLCVCandle,
        ts_key: str,
        buffers: dict[str, CandleBuffer],
        last_ts: dict[str, str],
        callback: CandleCallback | None,
    ):
        """Add candle to buffer and fire callback if it's a new candle."""
        if epic not in buffers:
            buffers[epic] = CandleBuffer()

        prev_ts = last_ts.get(epic, "")

        if ts_key != prev_ts:
            # New candle closed
            buffers[epic].append(candle)
            last_ts[epic] = ts_key
            if callback:
                try:
                    await callback(epic, candle)
                except Exception as e:
                    logger.error("Candle callback error for %s: %s", epic, e, exc_info=True)

    # ------------------------------------------------------------------
    # 15min REST polling fallback
    # ------------------------------------------------------------------

    async def _poll_15min_loop(self, epics: list[str]):
        """Poll for new 15min candles via REST if WebSocket doesn't support it."""
        logger.info("Starting 15min REST polling fallback (interval=%.0fs)", self._poll_15min_interval)
        while True:
            try:
                await asyncio.sleep(self._poll_15min_interval)
                for epic in epics:
                    await self._poll_15min_once(epic)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("15min poll error: %s", e)

    async def _poll_15min_once(self, epic: str):
        """Fetch latest 15min candles and check for new ones."""
        candles = await self._client.get_prices(
            epic, resolution="MINUTE_15", max_candles=5
        )
        if not candles:
            return

        for c in candles:
            ohlcv = OHLCVCandle.from_api_candle(c)
            ts_key = ohlcv.timestamp.isoformat()
            await self._process_candle(
                epic, ohlcv, ts_key,
                self._buffers_15min, self._last_15min_ts,
                self.on_15min_candle,
            )

    # ------------------------------------------------------------------
    # Public access to buffers
    # ------------------------------------------------------------------

    def get_1min_buffer(self, epic: str) -> CandleBuffer | None:
        return self._buffers_1min.get(epic)

    def get_15min_buffer(self, epic: str) -> CandleBuffer | None:
        return self._buffers_15min.get(epic)

    def get_daily_buffer(self, epic: str) -> CandleBuffer | None:
        return self._buffers_daily.get(epic)

    def get_last_1min(self, epic: str) -> OHLCVCandle | None:
        buf = self._buffers_1min.get(epic)
        return buf.last if buf else None

    def get_last_15min(self, epic: str) -> OHLCVCandle | None:
        buf = self._buffers_15min.get(epic)
        return buf.last if buf else None

    def get_avg_spread(self, epic: str) -> float | None:
        """
        Return average bid-ask spread computed from historical candles.
        Returns None if no spread data is available.
        """
        count = self._spread_counts.get(epic, 0)
        if count == 0:
            return None
        return self._spread_sums[epic] / count

    def update_spread(self, epic: str, spread: float):
        """Update running average with a new spread observation."""
        if spread <= 0:
            return
        self._spread_sums[epic] = self._spread_sums.get(epic, 0.0) + spread
        self._spread_counts[epic] = self._spread_counts.get(epic, 0) + 1
        # Cap at 500 samples to keep average recent (exponential decay would be
        # better, but simple cap is fine for now)
        count = self._spread_counts[epic]
        if count > 500:
            avg = self._spread_sums[epic] / count
            self._spread_sums[epic] = avg * 250
            self._spread_counts[epic] = 250

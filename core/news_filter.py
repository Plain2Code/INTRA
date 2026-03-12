"""
News filter – Forex Factory economic calendar integration.

Fetches high-impact economic events from the free Forex Factory API
and blocks trading during blackout windows around those events.

Loads both current week and next week calendars to ensure Monday
events are always available (even when starting the bot on weekends).

API: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Cost: Free, no API key required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import httpx

import config

logger = logging.getLogger(__name__)

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# Map our instrument epics to the currency codes that affect them
EPIC_CURRENCIES: dict[str, list[str]] = {
    "US100": ["USD"],
    "US500": ["USD"],
    "DE40":  ["EUR"],
    "FR40":  ["EUR"],
    "UK100": ["GBP"],
}


@dataclass
class NewsEvent:
    title: str
    country: str        # "USD", "EUR", etc.
    time: datetime      # UTC
    impact: str         # "High", "Medium", "Low", "Holiday"
    forecast: str
    previous: str


class NewsFilter:
    """
    Checks for upcoming high-impact economic events.

    Blocks trading within a configurable blackout window before/after
    high-impact events that are relevant to the active instruments.

    Usage:
        nf = NewsFilter()
        await nf.initialize()
        blocked, reason = nf.get_blocking_info(epic="US100")
    """

    def __init__(self):
        self._events: list[NewsEvent] = []
        self._last_fetch: datetime | None = None
        self._fetch_failures: int = 0

    async def initialize(self):
        """Fetch economic calendar from Forex Factory."""
        await self._fetch_events()
        # Retry once on failure with delay (e.g. rate limit or transient error)
        if not self._events and self._fetch_failures > 0:
            logger.debug("Retrying news fetch after 5s delay...")
            await asyncio.sleep(5)
            await self._fetch_events()

    async def refresh_if_needed(self):
        """Re-fetch if last fetch was more than NEWS_REFRESH_HOURS ago."""
        if self._last_fetch is None:
            await self._fetch_events()
            return
        elapsed = (datetime.now(timezone.utc) - self._last_fetch).total_seconds() / 3600
        if elapsed >= config.NEWS_REFRESH_HOURS:
            logger.debug("News calendar refresh (%.1fh since last fetch)", elapsed)
            await self._fetch_events()

    async def _fetch_events(self):
        """Download and parse Forex Factory calendar (this week + next week)."""
        all_events: list[NewsEvent] = []

        async with httpx.AsyncClient(timeout=15.0) as client:
            for i, url in enumerate(FF_URLS):
                if i > 0:
                    await asyncio.sleep(1)  # delay between requests
                try:
                    resp = await client.get(url)
                    if resp.status_code == 429:
                        logger.warning("Forex Factory rate limited (429) for %s", url)
                        self._fetch_failures += 1
                        continue
                    if resp.status_code == 404:
                        # nextweek.json often returns 404 on weekends – not an error
                        logger.debug("Forex Factory 404 for %s (expected on weekends)", url)
                        continue
                    resp.raise_for_status()
                    raw = resp.json()

                    for item in raw:
                        event = self._parse_event(item)
                        if event:
                            all_events.append(event)

                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", url, e)
                    self._fetch_failures += 1

        if all_events:
            # Deduplicate by (title, time) in case of overlap
            seen = set()
            unique = []
            for ev in all_events:
                key = (ev.title, ev.time.isoformat())
                if key not in seen:
                    seen.add(key)
                    unique.append(ev)
            self._events = unique
            self._last_fetch = datetime.now(timezone.utc)
            self._fetch_failures = 0

            high_count = sum(1 for e in self._events if e.impact == "High")
            med_count = sum(1 for e in self._events if e.impact == "Medium")
            logger.debug("News calendar: %d events (%d High, %d Medium)", len(self._events), high_count, med_count)

    @staticmethod
    def _parse_event(item: dict) -> NewsEvent | None:
        """Parse a single calendar item. Returns None if irrelevant."""
        impact = item.get("impact", "")
        if impact not in ("High", "Medium"):
            return None

        date_str = item.get("date", "")
        if not date_str:
            return None

        try:
            dt = datetime.fromisoformat(date_str)
            dt_utc = dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

        return NewsEvent(
            title=item.get("title", ""),
            country=item.get("country", ""),
            time=dt_utc,
            impact=impact,
            forecast=item.get("forecast", ""),
            previous=item.get("previous", ""),
        )

    def get_blocking_info(self, epic: str | None = None) -> tuple[bool, str]:
        """
        Check if trading is blocked by a news event.

        Returns (blocked, reason) where reason describes the specific
        blocking event with timing info.

        Blackout: NEWS_BLACKOUT_BEFORE_MINUTES before,
                  NEWS_BLACKOUT_AFTER_MINUTES after high-impact events.
        """
        now = datetime.now(timezone.utc)
        currencies = self._get_currencies(epic)

        for event in self._events:
            if event.impact != "High":
                continue
            if currencies and event.country not in currencies:
                continue

            minutes_until = (event.time - now).total_seconds() / 60

            if -config.NEWS_BLACKOUT_AFTER_MINUTES <= minutes_until <= config.NEWS_BLACKOUT_BEFORE_MINUTES:
                if minutes_until > 0:
                    reason = (
                        f"News blackout: '{event.title}' ({event.country}) "
                        f"in {int(minutes_until)}min"
                    )
                else:
                    reason = (
                        f"News blackout: '{event.title}' ({event.country}) "
                        f"was {int(abs(minutes_until))}min ago, "
                        f"post-event buffer active"
                    )
                logger.info("NEWS BLOCK: %s", reason)
                return True, reason

        return False, ""

    def is_blocked(self, epic: str | None = None) -> bool:
        """Return True if a high-impact event is within the blackout window."""
        blocked, _ = self.get_blocking_info(epic)
        return blocked

    def minutes_to_next_event(self, epic: str | None = None) -> int | None:
        """Return minutes until next high-impact event, or None."""
        now = datetime.now(timezone.utc)
        currencies = self._get_currencies(epic)
        closest = None

        for event in self._events:
            if event.impact != "High":
                continue
            if currencies and event.country not in currencies:
                continue

            delta = (event.time - now).total_seconds() / 60
            if delta > 0:
                if closest is None or delta < closest:
                    closest = delta

        return int(closest) if closest is not None else None

    def get_upcoming_events(self, limit: int = 10) -> list[dict]:
        """Return upcoming events for dashboard display."""
        now = datetime.now(timezone.utc)
        # On weekends, show recent events too (last 2 days) so calendar isn't empty
        is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
        cutoff = (now - timedelta(days=2)) if is_weekend else now.replace(hour=0, minute=0, second=0)
        upcoming = []

        for event in sorted(self._events, key=lambda e: e.time):
            if event.time < cutoff:
                continue

            delta_min = (event.time - now).total_seconds() / 60
            minutes_until = int(delta_min)
            if minutes_until > 0:
                if minutes_until >= 60:
                    countdown = f"{minutes_until // 60}h {minutes_until % 60}m"
                else:
                    countdown = f"{minutes_until}m"
            elif minutes_until > -config.NEWS_BLACKOUT_AFTER_MINUTES:
                countdown = "NOW"
            else:
                countdown = "passed"

            upcoming.append({
                "title": event.title,
                "country": event.country,
                "time": event.time.strftime("%H:%M UTC"),
                "date": event.time.strftime("%a %d %b"),
                "impact": event.impact,
                "forecast": event.forecast,
                "previous": event.previous,
                "countdown": countdown,
                "minutes_until": minutes_until,
                "is_blocking": (
                    event.impact == "High" and
                    -config.NEWS_BLACKOUT_AFTER_MINUTES <= minutes_until <= config.NEWS_BLACKOUT_BEFORE_MINUTES
                ),
            })

            if len(upcoming) >= limit:
                break

        return upcoming

    def _get_currencies(self, epic: str | None) -> list[str]:
        """Get relevant currency codes for an instrument."""
        if epic is None:
            return []
        return EPIC_CURRENCIES.get(epic, [])

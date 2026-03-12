"""
Capital.com API client – REST + WebSocket.

Standalone module: handles authentication, session refresh, all REST
endpoints, and WebSocket streaming.  Every public method is independently
callable and testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import httpx
import websockets
import websockets.asyncio.client

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes for API responses
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    account_id: str
    balance: float
    deposit: float
    profit_loss: float
    available: float
    currency: str


@dataclass
class Position:
    deal_id: str
    epic: str
    direction: str          # "BUY" or "SELL"
    size: float
    open_level: float
    current_level: float | None = None
    stop_level: float | None = None
    profit_level: float | None = None
    trailing_stop: bool = False
    trailing_stop_distance: float | None = None  # set by Capital.com when trailing active
    profit_loss: float = 0.0
    created_date: str = ""


@dataclass
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float = 0.0  # bid-ask spread (ask_close - bid_close)


@dataclass
class DealConfirmation:
    deal_id: str
    deal_reference: str
    status: str        # "OPEN", "CLOSED", etc.
    deal_status: str   # "ACCEPTED" or "REJECTED"
    level: float
    size: float
    direction: str
    reason: str = ""


@dataclass
class Transaction:
    reference: str
    transaction_type: str
    instrument_name: str
    open_level: float | None = None
    close_level: float | None = None
    profit_loss: float = 0.0
    date: str = ""
    epic: str = ""
    deal_id: str = ""


# ---------------------------------------------------------------------------
# Capital.com client
# ---------------------------------------------------------------------------

class CapitalClient:
    """Async client for Capital.com REST + WebSocket APIs."""

    def __init__(self, mode: str | None = None):
        self._mode = mode or config.CAPITAL_MODE
        urls = config.API_URLS[self._mode]
        self._rest_url = urls["rest"]
        self._ws_url = urls["ws"]

        self._cst: str | None = None
        self._security_token: str | None = None
        self._last_request_time: float = 0.0
        self._session_created_at: float = 0.0

        self._semaphore = asyncio.Semaphore(config.MAX_REQUESTS_PER_SECOND)
        self._session_refresh_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

        # WebSocket state
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._ws_running = False
        self._ws_correlation_id = 0

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def close(self):
        """Shut down HTTP client and WebSocket."""
        self._ws_running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Auth headers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._cst:
            headers["CST"] = self._cst
        if self._security_token:
            headers["X-SECURITY-TOKEN"] = self._security_token
        return headers

    # ------------------------------------------------------------------
    # Rate-limited request
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Rate-limited HTTP request with auto session refresh."""
        async with self._semaphore:
            # enforce min gap between requests
            now = time.monotonic()
            gap = 1.0 / config.MAX_REQUESTS_PER_SECOND
            elapsed = now - self._last_request_time
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            self._last_request_time = time.monotonic()

        # auto-refresh session if close to expiry
        if self._cst and (time.monotonic() - self._session_created_at) > config.SESSION_TIMEOUT_SECONDS:
            await self._refresh_session()

        client = await self._ensure_http()
        url = f"{self._rest_url}{path}"
        headers = self._auth_headers()

        resp = await client.request(
            method, url, headers=headers, json=json_body, params=params
        )

        if resp.status_code == 401:
            logger.warning("Session expired, re-authenticating...")
            await self.login()
            headers = self._auth_headers()
            resp = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )

        if resp.status_code >= 400:
            logger.error("API error %s %s: %s %s", method, path, resp.status_code, resp.text)

        return resp

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self) -> AccountInfo:
        """Create a new session and return account info."""
        client = await self._ensure_http()
        url = f"{self._rest_url}/session"
        headers = {
            "X-CAP-API-KEY": config.CAPITAL_API_KEY,
            "Content-Type": "application/json",
        }
        body = {
            "identifier": config.CAPITAL_EMAIL,
            "password": config.CAPITAL_PASSWORD,
            "encryptedPassword": False,
        }

        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()

        self._cst = resp.headers.get("CST", "")
        self._security_token = resp.headers.get("X-SECURITY-TOKEN", "")
        self._session_created_at = time.monotonic()

        data = resp.json()
        info = data.get("accountInfo", {})

        # Use streaming host from API response (more reliable than hardcoded)
        streaming_host = data.get("streamingHost", "")
        if streaming_host:
            self._ws_url = streaming_host.rstrip("/") + "/connect"

        logger.info("Logged in to Capital.com (%s mode)", self._mode)

        return AccountInfo(
            account_id=data.get("currentAccountId", ""),
            balance=info.get("balance", 0.0),
            deposit=info.get("deposit", 0.0),
            profit_loss=info.get("profitLoss", 0.0),
            available=info.get("available", 0.0),
            currency=data.get("currencyIsoCode", "USD"),
        )

    async def _refresh_session(self):
        """Ping to keep the session alive. Lock prevents concurrent refreshes."""
        async with self._session_refresh_lock:
            # Double-check: another coroutine may have already refreshed while waiting for lock
            if (time.monotonic() - self._session_created_at) <= config.SESSION_TIMEOUT_SECONDS:
                return
            try:
                # Direct HTTP call – avoids recursion through _request()
                client = await self._ensure_http()
                resp = await client.request(
                    "GET", f"{self._rest_url}/ping", headers=self._auth_headers()
                )
                if resp.status_code < 400:
                    self._session_created_at = time.monotonic()
                    logger.debug("Session refreshed via ping")
                    return
            except Exception:
                pass
            logger.warning("Ping failed, re-logging in")
            await self.login()

    async def logout(self):
        """Destroy the current session."""
        await self._request("DELETE", "/session")
        self._cst = None
        self._security_token = None

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    async def get_accounts(self) -> list[AccountInfo]:
        resp = await self._request("GET", "/accounts")
        resp.raise_for_status()
        accounts = []
        for acc in resp.json().get("accounts", []):
            bal = acc.get("balance", {})
            accounts.append(AccountInfo(
                account_id=acc.get("accountId", ""),
                balance=bal.get("balance", 0.0),
                deposit=bal.get("deposit", 0.0),
                profit_loss=bal.get("profitLoss", 0.0),
                available=bal.get("available", 0.0),
                currency=acc.get("currency", "USD"),
            ))
        return accounts

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        resp = await self._request("GET", "/positions")
        resp.raise_for_status()
        positions = []
        for item in resp.json().get("positions", []):
            pos = item.get("position", {})
            market = item.get("market", {})
            positions.append(Position(
                deal_id=pos.get("dealId", ""),
                epic=market.get("epic", ""),
                direction=pos.get("direction", ""),
                size=pos.get("size", 0.0),
                open_level=pos.get("level", 0.0),
                current_level=market.get("bid", None),
                stop_level=pos.get("stopLevel", None),
                profit_level=pos.get("profitLevel", None),
                trailing_stop=pos.get("trailingStop", False),
                trailing_stop_distance=pos.get("trailingStopDistance", None),
                profit_loss=pos.get("upl", 0.0),
                created_date=pos.get("createdDateUTC", ""),
            ))
        return positions

    async def open_position(
        self,
        epic: str,
        direction: str,
        size: float,
        stop_level: float | None = None,
        stop_distance: float | None = None,
        profit_level: float | None = None,
        profit_distance: float | None = None,
        trailing_stop: bool = False,
    ) -> str:
        """Open a position. Returns the deal reference."""
        body: dict[str, Any] = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": False,
            "trailingStop": trailing_stop,
        }
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if stop_distance is not None:
            body["stopDistance"] = stop_distance
        if profit_level is not None:
            body["profitLevel"] = profit_level
        if profit_distance is not None:
            body["profitDistance"] = profit_distance

        resp = await self._request("POST", "/positions", json_body=body)
        resp.raise_for_status()
        deal_ref = resp.json().get("dealReference", "")
        logger.debug("API open_position: %s %s size=%.2f ref=%s", epic, direction, size, deal_ref)
        return deal_ref

    async def modify_position(
        self,
        deal_id: str,
        *,
        stop_level: float | None = None,
        profit_level: float | None = None,
        trailing_stop: bool | None = None,
        trailing_stop_distance: float | None = None,
    ) -> bool:
        """Modify an open position (SL/TP/trailing). Returns success."""
        body: dict[str, Any] = {"guaranteedStop": False}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        if trailing_stop is not None:
            body["trailingStop"] = trailing_stop
        if trailing_stop_distance is not None:
            body["stopDistance"] = trailing_stop_distance

        resp = await self._request("PUT", f"/positions/{deal_id}", json_body=body)
        ok = resp.status_code < 400
        if ok:
            logger.debug("Modified position %s", deal_id)
        return ok

    async def close_position(self, deal_id: str) -> bool:
        """Close (delete) a position. Returns success."""
        resp = await self._request("DELETE", f"/positions/{deal_id}")
        ok = resp.status_code < 400
        if ok:
            logger.debug("API close_position %s", deal_id)
        return ok

    # ------------------------------------------------------------------
    # Deal confirmation
    # ------------------------------------------------------------------

    async def get_confirmation(self, deal_reference: str) -> DealConfirmation | None:
        resp = await self._request("GET", f"/confirms/{deal_reference}")
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return DealConfirmation(
            deal_id=data.get("dealId", ""),
            deal_reference=deal_reference,
            status=data.get("status", ""),
            deal_status=data.get("dealStatus", ""),
            level=data.get("level", 0.0),
            size=data.get("size", 0.0),
            direction=data.get("direction", ""),
            reason=data.get("reason", ""),
        )

    # ------------------------------------------------------------------
    # Historical prices
    # ------------------------------------------------------------------

    async def get_prices(
        self,
        epic: str,
        resolution: str = "MINUTE",
        max_candles: int = 500,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[Candle]:
        """Fetch historical OHLCV candles."""
        params: dict[str, Any] = {
            "resolution": resolution,
            "max": max_candles,
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        resp = await self._request("GET", f"/prices/{epic}", params=params)
        resp.raise_for_status()
        candles = []
        for p in resp.json().get("prices", []):
            bid = p.get("closePrice", {})
            ask = p.get("closePrice", {})
            # Capital.com returns bid/ask OHLC – use mid prices
            snap = p.get("snapshotTimeUTC", p.get("snapshotTime", ""))
            o_bid = p.get("openPrice", {}).get("bid", 0)
            o_ask = p.get("openPrice", {}).get("ask", 0)
            h_bid = p.get("highPrice", {}).get("bid", 0)
            h_ask = p.get("highPrice", {}).get("ask", 0)
            l_bid = p.get("lowPrice", {}).get("bid", 0)
            l_ask = p.get("lowPrice", {}).get("ask", 0)
            c_bid = p.get("closePrice", {}).get("bid", 0)
            c_ask = p.get("closePrice", {}).get("ask", 0)

            candles.append(Candle(
                timestamp=snap,
                open=(o_bid + o_ask) / 2,
                high=(h_bid + h_ask) / 2,
                low=(l_bid + l_ask) / 2,
                close=(c_bid + c_ask) / 2,
                volume=float(p.get("lastTradedVolume", 0)),
                spread=max(c_ask - c_bid, 0.0),
            ))
        return candles

    # ------------------------------------------------------------------
    # Transaction history
    # ------------------------------------------------------------------

    async def get_transactions(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        max_results: int = 500,
    ) -> list[Transaction]:
        """Fetch recent transaction history."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        resp = await self._request("GET", "/history/transactions", params=params)
        if resp.status_code >= 400:
            return []

        transactions = []
        raw_txns = resp.json().get("transactions", [])
        for t in raw_txns:
            # Capital.com uses "transactionType" (not "type"), and P&L is
            # in the "size" field as a string in account currency (EUR).
            pnl_raw = t.get("profitAndLoss") or t.get("size") or "0"
            pnl_str = str(pnl_raw).replace(",", "").replace("USD", "").replace("EUR", "").replace("EURd", "").strip()
            try:
                pnl_val = float(pnl_str) if pnl_str else 0.0
            except ValueError:
                pnl_val = 0.0

            transactions.append(Transaction(
                reference=t.get("reference", ""),
                transaction_type=t.get("transactionType") or t.get("type", ""),
                instrument_name=t.get("instrumentName", ""),
                open_level=t.get("openLevel", None),
                close_level=t.get("closeLevel", None),
                profit_loss=pnl_val,
                date=t.get("dateUtc", ""),
                epic=t.get("epic", ""),
                deal_id=t.get("dealId", ""),
            ))
        return transactions

    # ------------------------------------------------------------------
    # Market search (discover epics)
    # ------------------------------------------------------------------

    async def search_markets(self, search_term: str) -> list[dict]:
        resp = await self._request("GET", "/markets", params={"searchTerm": search_term})
        if resp.status_code >= 400:
            return []
        return resp.json().get("markets", [])

    async def get_market_details(self, epic: str) -> dict | None:
        resp = await self._request("GET", f"/markets/{epic}")
        if resp.status_code >= 400:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # WebSocket streaming
    # ------------------------------------------------------------------

    def _next_correlation_id(self) -> str:
        self._ws_correlation_id += 1
        return str(self._ws_correlation_id)

    async def ws_connect(self):
        """Establish WebSocket connection."""
        self._ws = await websockets.asyncio.client.connect(self._ws_url)
        self._ws_running = True
        logger.debug("WebSocket connected")

    async def ws_subscribe_ohlc(
        self, epics: list[str], resolutions: list[str] | None = None
    ):
        """Subscribe to OHLC candle updates."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        if resolutions is None:
            resolutions = ["MINUTE"]

        msg = {
            "destination": "OHLCMarketData.subscribe",
            "correlationId": self._next_correlation_id(),
            "cst": self._cst,
            "securityToken": self._security_token,
            "payload": {
                "epics": epics,
                "resolutions": resolutions,
                "type": "classic",
            },
        }
        await self._ws.send(json.dumps(msg))
        logger.debug("Subscribed OHLC: %s %s", epics, resolutions)

    async def ws_subscribe_prices(self, epics: list[str]):
        """Subscribe to real-time bid/ask price updates."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        msg = {
            "destination": "marketData.subscribe",
            "correlationId": self._next_correlation_id(),
            "cst": self._cst,
            "securityToken": self._security_token,
            "payload": {
                "epics": epics,
            },
        }
        await self._ws.send(json.dumps(msg))
        logger.debug("Subscribed prices: %s", epics)

    async def ws_ping(self):
        """Send keep-alive ping."""
        if self._ws is None:
            return
        msg = {
            "destination": "ping",
            "correlationId": self._next_correlation_id(),
            "cst": self._cst,
            "securityToken": self._security_token,
        }
        await self._ws.send(json.dumps(msg))

    async def ws_listen(
        self,
        on_message: Callable[[dict], Coroutine[Any, Any, None]],
    ):
        """
        Listen for WebSocket messages and dispatch to callback.
        Runs until ws_disconnect() is called or connection drops.
        Automatically sends ping every 9 minutes.
        """
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        async def ping_loop():
            while self._ws_running:
                await asyncio.sleep(540)  # 9 minutes
                try:
                    await self.ws_ping()
                except Exception:
                    break

        ping_task = asyncio.create_task(ping_loop())
        msg_count = 0

        try:
            async for raw in self._ws:
                if not self._ws_running:
                    break
                msg_count += 1
                try:
                    data = json.loads(raw)
                    await on_message(data)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON WebSocket message: %s", raw[:200])
                except Exception as e:
                    logger.error("Error handling WS message: %s", e, exc_info=True)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("WebSocket closed: %s (after %d messages)", e, msg_count)
        finally:
            ping_task.cancel()

    async def ws_disconnect(self):
        """Disconnect WebSocket."""
        self._ws_running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
            logger.debug("WebSocket disconnected")

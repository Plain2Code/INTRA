"""
Dashboard API – FastAPI backend with WebSocket live updates.

The dashboard is the control center: it starts/stops the bot,
selects instruments, and shows live status.  The bot does NOT
auto-start – the user must press "Start" in the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orchestrator import Orchestrator
import config

logger = logging.getLogger(__name__)

app = FastAPI(title="INTRA Trading Bot Dashboard")

# Global state
_orchestrator: Orchestrator | None = None
_bot_task: asyncio.Task | None = None
_allow_live: bool = False
_ws_clients: set[WebSocket] = set()

STATIC_DIR = Path(__file__).parent / "static"
ACTIVE_ASSETS_PATH = Path(config.ACTIVE_ASSETS_FILE)


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

async def broadcast(data: dict):
    if not _ws_clients:
        return
    message = json.dumps(data)
    disconnected = []
    for ws in list(_ws_clients):
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        _ws_clients.discard(ws)


def on_status_update(status: dict):
    asyncio.ensure_future(broadcast({"type": "status", "data": status}))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    instruments: list[str]
    live: bool = False
    use_ws_15min: bool = True


class SaveAssetsRequest(BaseModel):
    assets: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_session(inst: config.Instrument) -> str:
    sess = config.INSTRUMENT_SESSIONS.get(inst)
    if not sess:
        return "Unknown"
    return f"{sess.open_time.strftime('%H:%M')}-{sess.close_time.strftime('%H:%M')} {sess.timezone}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>INTRA Dashboard</h1><p>static/index.html not found</p>")


@app.get("/api/status")
async def get_status():
    if _orchestrator:
        return _orchestrator.get_full_status()
    return {
        "running": False,
        "mode": "DEMO",
        "balance": 0,
        "equity": 0,
        "margin_used": 0,
        "available": 0,
        "daily_pnl": 0,
        "daily_pnl_pct": 0,
        "trade_count": 0,
        "biases": {},
        "kill_switch": False,
        "instruments": [],
        "date": "",
        "equity_history": [],
        "last_setup": None,
        "last_setups": {},
        "winrate_stats": {},
        "winrate_stats_by_epic": {},
        "epic_currencies": {},
        "recent_trades": [],
        "open_position": None,
        "open_positions": {},
        "news_events": [],
        "pipeline_matrix": {},
        "consecutive_losses": 0,
        "allow_live": _allow_live,
    }


@app.get("/api/config")
async def get_config():
    """Return available instruments and settings for the UI."""
    return {
        "allow_live": _allow_live,
        "instruments": [
            {
                "epic": inst.value,
                "name": inst.name.replace("_", " ").title(),
                "session": _format_session(inst),
            }
            for inst in config.Instrument
        ],
        "bot_running": _orchestrator is not None and _orchestrator._state.is_running,
    }


@app.get("/api/active-assets")
async def get_active_assets():
    """Load previously saved asset selection."""
    if ACTIVE_ASSETS_PATH.exists():
        try:
            data = json.loads(ACTIVE_ASSETS_PATH.read_text())
            return {"assets": data.get("assets", [])}
        except (json.JSONDecodeError, IOError):
            pass
    return {"assets": []}


@app.post("/api/active-assets")
async def save_active_assets(req: SaveAssetsRequest):
    """Save selected assets to persistent config."""
    try:
        ACTIVE_ASSETS_PATH.write_text(json.dumps({"assets": req.assets}, indent=2))
        return {"status": "saved", "assets": req.assets}
    except IOError as e:
        return {"error": f"Failed to save: {e}"}


@app.post("/api/start")
async def start_bot(req: StartRequest):
    """Start the trading bot with selected instruments."""
    global _orchestrator, _bot_task

    if _orchestrator and _orchestrator._state.is_running:
        return {"error": "Bot is already running"}

    if req.live and not _allow_live:
        return {"error": "Live mode not allowed. Start with --live flag."}

    if not req.instruments:
        return {"error": "No instruments selected"}

    _orchestrator = Orchestrator(
        instruments=req.instruments,
        is_live=req.live,
        use_ws_15min=req.use_ws_15min,
    )
    _orchestrator.on_status_update = on_status_update

    _bot_task = asyncio.create_task(_run_bot())

    return {"status": "starting", "instruments": req.instruments, "mode": "LIVE" if req.live else "DEMO"}


async def _run_bot():
    """Run the bot in a background task."""
    try:
        await _orchestrator.start()
    except Exception as e:
        logger.error("Bot crashed: %s", e, exc_info=True)
        await broadcast({"type": "error", "data": {"message": str(e)}})


@app.post("/api/stop")
async def stop_bot():
    if _orchestrator:
        await _orchestrator.stop()
        return {"status": "stopping"}
    return {"error": "Bot not running"}


@app.post("/api/kill-switch")
async def kill_switch():
    if _orchestrator:
        await _orchestrator.kill_switch()
        return {"status": "kill switch activated"}
    return {"error": "Bot not running"}


@app.post("/api/restart")
async def restart_bot(req: StartRequest):
    """Stop the current bot and start a new one with different settings."""
    global _orchestrator, _bot_task

    if _orchestrator:
        await _orchestrator.stop()
        # Give it a moment to shut down
        await asyncio.sleep(1)

    _orchestrator = Orchestrator(
        instruments=req.instruments,
        is_live=req.live and _allow_live,
        use_ws_15min=req.use_ws_15min,
    )
    _orchestrator.on_status_update = on_status_update
    _bot_task = asyncio.create_task(_run_bot())

    return {"status": "restarting", "instruments": req.instruments,
            "mode": "LIVE" if (req.live and _allow_live) else "DEMO"}


@app.get("/api/log-level")
async def get_log_level():
    """Return the current root log level."""
    return {"level": logging.getLevelName(logging.getLogger().level)}


class LogLevelRequest(BaseModel):
    level: str


@app.post("/api/log-level")
async def set_log_level(req: LogLevelRequest):
    """Change the root log level at runtime."""
    level_name = req.level.upper()
    numeric = getattr(logging, level_name, None)
    if numeric is None:
        return {"error": f"Unknown log level: {req.level}"}
    logging.getLogger().setLevel(numeric)
    logger.info("Log level changed to %s", level_name)
    return {"level": level_name}


@app.get("/api/logs")
async def get_logs(lines: int = 200):
    """Return the last N lines of intra.log."""
    log_path = Path("intra.log")
    if not log_path.exists():
        return {"lines": []}
    try:
        content = log_path.read_text(errors="replace")
        all_lines = content.splitlines()
        return {"lines": all_lines[-lines:]}
    except IOError:
        return {"lines": []}


@app.get("/api/trades")
async def get_trades():
    if _orchestrator and _orchestrator._tracker:
        return {"trades": _orchestrator._tracker.get_recent_trades(50)}
    return {"trades": []}


@app.get("/api/stats")
async def get_stats():
    if _orchestrator and _orchestrator._tracker:
        return {"stats": _orchestrator._tracker.get_all_stats()}
    return {"stats": {}}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    logger.info("Dashboard client connected (total: %d)", len(_ws_clients))

    # Send initial status
    status = await get_status()
    try:
        await ws.send_text(json.dumps({"type": "status", "data": status}))
    except Exception:
        pass

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                cmd = msg.get("command")
                if cmd == "refresh":
                    status = await get_status()
                    await ws.send_text(json.dumps({"type": "status", "data": status}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        logger.info("Dashboard client disconnected (total: %d)", len(_ws_clients))


# ---------------------------------------------------------------------------
# Periodic status push
# ---------------------------------------------------------------------------

async def _status_push_loop():
    while True:
        await asyncio.sleep(5)
        if _orchestrator and _ws_clients:
            try:
                status = _orchestrator.get_full_status()
                await broadcast({"type": "status", "data": status})
            except Exception as e:
                logger.error("Status push error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_dashboard(
    host: str = config.DASHBOARD_HOST,
    port: int = config.DASHBOARD_PORT,
    allow_live: bool = False,
):
    """Run the dashboard server (called from main.py)."""
    global _allow_live
    _allow_live = allow_live

    cfg = uvicorn.Config(
        app, host=host, port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(cfg)

    push_task = asyncio.create_task(_status_push_loop())
    logger.info("Dashboard running at http://%s:%d", host, port)

    try:
        await server.serve()
    finally:
        push_task.cancel()

"""
INTRA Trading Bot – Entry point.

Usage:
    python main.py                # Start dashboard, control bot from browser
    python main.py --live         # Allow live mode in dashboard
    python main.py --port 9090    # Custom dashboard port
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import config


def setup_logging():
    """Configure structured logging."""
    fmt = (
        "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("intra.log", mode="a"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="INTRA Intraday Trading Bot")
    parser.add_argument(
        "--live", action="store_true",
        help="Allow LIVE mode selection in dashboard. Default is DEMO only.",
    )
    parser.add_argument(
        "--port", type=int, default=config.DASHBOARD_PORT,
        help=f"Dashboard port (default: {config.DASHBOARD_PORT}).",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace):
    """Start dashboard only – bot is controlled from the browser."""
    from dashboard.api import run_dashboard
    await run_dashboard(
        port=args.port,
        allow_live=args.live,
    )


def main():
    setup_logging()
    args = parse_args()

    logger = logging.getLogger(__name__)
    logger.info("=" * 50)
    logger.info("  INTRA Trading Bot")
    logger.info("  Dashboard: http://127.0.0.1:%d", args.port)
    logger.info("  Mode: %s", "LIVE allowed" if args.live else "DEMO only")
    logger.info("=" * 50)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()

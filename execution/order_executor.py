"""
Order executor – order lifecycle management.

Standalone module: handles opening positions, confirming deals,
and closing positions via the Capital.com API.
Exit decisions are made by the orchestrator's staged exit management.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from core.capital_client import CapitalClient, DealConfirmation
import config

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    deal_id: str
    deal_reference: str
    message: str
    level: float = 0.0


class OrderExecutor:
    """
    Handles the full order lifecycle:
    1. Open position with SL + TP (emergency backstops only)
    2. Confirm deal acceptance
    3. Close position on ExitEngine signal or kill switch
    """

    def __init__(self, client: CapitalClient):
        self._client = client

    async def open_trade(
        self,
        epic: str,
        direction: str,
        size: float,
        sl_price: float,
        tp_price: float | None = None,
    ) -> OrderResult:
        """
        Open a new position with stop loss and optional take profit.

        Args:
            epic: Instrument epic code
            direction: "BUY" or "SELL"
            size: Position size
            sl_price: Stop loss price level
            tp_price: Take profit price level (None = no TP, exits via trailing/EOD)

        Returns:
            OrderResult with deal ID and status
        """
        try:
            deal_ref = await self._client.open_position(
                epic=epic,
                direction=direction,
                size=size,
                stop_level=sl_price,
                profit_level=tp_price,
            )

            if not deal_ref:
                return OrderResult(False, "", "", "No deal reference returned")

            # Wait briefly for order processing
            await asyncio.sleep(0.5)

            # Confirm the deal
            confirmation = await self._client.get_confirmation(deal_ref)

            if confirmation is None:
                return OrderResult(
                    False, "", deal_ref,
                    "Could not get deal confirmation"
                )

            if confirmation.deal_status == "REJECTED":
                logger.warning(
                    "Order REJECTED: %s %s %s – reason: %s",
                    epic, direction, deal_ref, confirmation.reason,
                )
                return OrderResult(
                    False, "", deal_ref,
                    f"Order rejected: {confirmation.reason}"
                )

            logger.debug("Order accepted: %s %s dealId=%s", epic, direction, confirmation.deal_id)

            return OrderResult(
                success=True,
                deal_id=confirmation.deal_id,
                deal_reference=deal_ref,
                message="Order accepted",
                level=confirmation.level,
            )

        except Exception as e:
            logger.error("Order execution failed: %s", e, exc_info=True)
            return OrderResult(False, "", "", f"Exception: {e}")

    async def close_trade(self, deal_id: str) -> bool:
        """Close a position by deal ID."""
        try:
            success = await self._client.close_position(deal_id)
            if success:
                logger.debug("Position %s closed", deal_id)
            return success
        except Exception as e:
            logger.error("Failed to close position %s: %s", deal_id, e)
            return False

    async def close_all_positions(self) -> int:
        """
        Close all open positions (kill switch).
        Returns the number of positions closed.
        """
        positions = await self._client.get_positions()
        closed = 0
        for pos in positions:
            if await self.close_trade(pos.deal_id):
                closed += 1
        logger.warning("Kill switch: closed %d/%d positions", closed, len(positions))
        return closed

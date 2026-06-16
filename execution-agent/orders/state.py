"""
orders/state.py — In-memory tracking of open positions and order state.

The execution agent is single-position: at most one spread (or iron condor)
is open at any given time per day. This module tracks:
  - The current open position (if any)
  - Entry/exit times and prices
  - Running P&L for the open position

The watchdog and main loop use this to decide when to monitor for exits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pytz

from orders.manager import OrderResult, OrderStatus

LOGGER = logging.getLogger("infiniteloop.orders.state")

EASTERN = pytz.timezone("US/Eastern")


@dataclass
class OpenPosition:
    """A spread position currently held."""
    entry_order: OrderResult
    entry_credit: float         # points received at entry
    entry_time: datetime
    direction: str              # UP / DOWN / NEUTRAL
    spread_type: str
    spread_width: float
    contracts: int

    # Filled in on exit
    exit_time: Optional[datetime] = None
    exit_debit: Optional[float] = None    # cost to close (points paid)
    exit_reason: Optional[str] = None     # profit_target / stop_loss / forced_exit / watchdog

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def unrealized_pnl_points(self) -> Optional[float]:
        """Can't compute without a current market quote — returns None."""
        return None

    @property
    def realized_pnl_points(self) -> Optional[float]:
        if self.exit_debit is None:
            return None
        return self.entry_credit - self.exit_debit

    @property
    def realized_pnl_dollars(self) -> Optional[float]:
        pts = self.realized_pnl_points
        if pts is None:
            return None
        from constants import SPX_MULTIPLIER
        return round(pts * self.contracts * SPX_MULTIPLIER, 2)

    def to_dict(self) -> dict:
        d = {
            "order_id": self.entry_order.order_id,
            "direction": self.direction,
            "spread_type": self.spread_type,
            "spread_width": self.spread_width,
            "contracts": self.contracts,
            "entry_credit": round(self.entry_credit, 2),
            "entry_time": self.entry_time.isoformat(),
            "is_open": self.is_open,
        }
        if not self.is_open:
            d.update({
                "exit_debit": round(self.exit_debit, 2) if self.exit_debit is not None else None,
                "exit_time": self.exit_time.isoformat() if self.exit_time else None,
                "exit_reason": self.exit_reason,
                "realized_pnl_points": self.realized_pnl_points,
                "realized_pnl_dollars": self.realized_pnl_dollars,
            })
        return d


class PositionState:
    """
    Single-position tracker for the execution agent.

    At most one position is open at a time per trading day.
    The state resets each morning via reset().
    """

    def __init__(self) -> None:
        self._position: Optional[OpenPosition] = None
        self._daily_pnl_dollars: float = 0.0
        self._daily_trades: list[OpenPosition] = []

    # ── Position lifecycle ────────────────────────────────────────────────────

    def record_entry(
        self,
        order: OrderResult,
        direction: str,
        spread_width: float,
    ) -> OpenPosition:
        """Call after a fill is confirmed. Raises if a position is already open."""
        if self._position and self._position.is_open:
            raise RuntimeError(
                f"Cannot open new position — {self._position.spread_type} already open "
                f"(order {self._position.entry_order.order_id})"
            )
        pos = OpenPosition(
            entry_order=order,
            entry_credit=order.net_credit,
            entry_time=order.filled_at or datetime.now(EASTERN),
            direction=direction,
            spread_type=order.spread_type,
            spread_width=spread_width,
            contracts=order.contracts,
        )
        self._position = pos
        LOGGER.info(
            "Position opened: %s × %d @ %.2f pts (order %s, paper=%s)",
            pos.spread_type, pos.contracts, pos.entry_credit,
            order.order_id, order.is_paper,
        )
        return pos

    def record_exit(
        self,
        exit_debit: float,
        reason: str,
        exit_time: Optional[datetime] = None,
    ) -> Optional[OpenPosition]:
        """
        Close the current open position.
        exit_debit = cost to close in SPX points (positive = paid to close).
        Returns the closed position, or None if nothing was open.
        """
        if self._position is None or not self._position.is_open:
            LOGGER.warning("record_exit called but no open position")
            return None

        self._position.exit_debit  = exit_debit
        self._position.exit_time   = exit_time or datetime.now(EASTERN)
        self._position.exit_reason = reason

        pnl = self._position.realized_pnl_dollars or 0.0
        self._daily_pnl_dollars += pnl
        self._daily_trades.append(self._position)

        LOGGER.info(
            "Position closed: %s — reason=%s, P&L=%.2f pts ($%.2f), daily_pnl=$%.2f",
            self._position.spread_type, reason,
            self._position.realized_pnl_points or 0.0, pnl,
            self._daily_pnl_dollars,
        )
        closed = self._position
        self._position = None
        return closed

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def has_open_position(self) -> bool:
        return self._position is not None and self._position.is_open

    @property
    def current_position(self) -> Optional[OpenPosition]:
        return self._position if self.has_open_position else None

    @property
    def daily_pnl_dollars(self) -> float:
        return self._daily_pnl_dollars

    @property
    def daily_trades(self) -> list[OpenPosition]:
        return list(self._daily_trades)

    @property
    def traded_today(self) -> bool:
        """True if we've completed at least one trade today (open or closed)."""
        return bool(self._daily_trades) or self.has_open_position

    # ── Daily reset ───────────────────────────────────────────────────────────

    def reset(self) -> None:
        """
        Called at the start of each trading day.
        Clears position and daily P&L. Previous trades are lost from memory
        (they're persisted to PostgreSQL by trade_logger.py).
        """
        if self._position and self._position.is_open:
            LOGGER.critical(
                "Position state reset with OPEN position! "
                "Order %s should have been closed. Forcing flat.",
                self._position.entry_order.order_id,
            )
        self._position = None
        self._daily_pnl_dollars = 0.0
        self._daily_trades = []
        LOGGER.info("PositionState reset for new trading day")

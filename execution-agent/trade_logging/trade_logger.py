"""
logging/trade_logger.py — Write confirmed fills and daily equity snapshots to PostgreSQL.

All DB writes happen AFTER a trade is confirmed filled (not on signal, not on order
submission). All writes use parameterized queries. Multi-table writes use transactions.

Tables written:
  - trades:           One row per fill (entry or exit leg)
  - equity_snapshots: End-of-day equity snapshot

Schema mirrors docs/ARCHITECTURE.md. The strategy_id references strategies.id
so Portfolio Manager (Layer 3) can join them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import pytz

import config
from orders.manager import OrderResult, OrderStatus
from orders.state import OpenPosition

LOGGER = logging.getLogger("infiniteloop.logging.trade_logger")

EASTERN = pytz.timezone("US/Eastern")


class TradeLogger:
    """
    Synchronous PostgreSQL writer for trade events.

    Uses psycopg2 (synchronous) rather than asyncpg because writes happen
    in discrete events (on fill), not in the hot tick path. Keeps the async
    main loop clean.
    """

    def __init__(self, database_url: Optional[str] = None) -> None:
        self._url = database_url or config.DATABASE_URL
        self._conn = None

    def _get_conn(self):
        """Get or reconnect the DB connection."""
        import psycopg2
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._url, connect_timeout=10)
        return self._conn

    def log_entry(
        self,
        order: OrderResult,
        strategy_id: int,
        direction: str,
        spread_width: float,
        spx_price: float,
        vix: float,
        expected_move: float,
        oracle_confidence: float,
    ) -> Optional[int]:
        """
        Write an entry fill to the trades table.
        Returns the new trade row id, or None on failure.
        """
        conn = self._get_conn()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO trades (
                        strategy_id, order_id, trade_date, direction,
                        spread_type, spread_width, contracts,
                        entry_credit_pts, entry_credit_usd,
                        max_loss_usd, spx_price_at_entry, vix_at_entry,
                        expected_move, oracle_confidence,
                        is_paper, status, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, NOW()
                    )
                    RETURNING id
                    """,
                    (
                        strategy_id, order.order_id,
                        datetime.now(EASTERN).date(), direction,
                        order.spread_type, spread_width, order.contracts,
                        order.net_credit, order.net_credit_dollars,
                        order.max_loss_dollars, spx_price, vix,
                        expected_move, oracle_confidence,
                        order.is_paper, order.status.value,
                    ),
                )
                row_id = cur.fetchone()[0]
                LOGGER.info("Trade entry logged: trades.id=%d order=%s", row_id, order.order_id)
                return row_id
        except Exception as exc:
            LOGGER.error("Failed to log trade entry: %s", exc)
            return None

    def log_exit(
        self,
        trade_id: int,
        position: OpenPosition,
        current_equity: float,
    ) -> bool:
        """
        Update the trades row with exit information.
        Returns True on success.
        """
        if position.realized_pnl_dollars is None:
            LOGGER.warning("log_exit called with no realized P&L on trade_id=%d", trade_id)
            return False

        conn = self._get_conn()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE trades
                    SET exit_debit_pts    = %s,
                        exit_debit_usd    = %s,
                        realized_pnl_usd  = %s,
                        exit_reason       = %s,
                        exit_time         = %s,
                        status            = 'CLOSED',
                        equity_after      = %s
                    WHERE id = %s
                    """,
                    (
                        position.exit_debit,
                        (position.exit_debit or 0) * position.contracts * 100,
                        position.realized_pnl_dollars,
                        position.exit_reason,
                        position.exit_time,
                        current_equity,
                        trade_id,
                    ),
                )
                LOGGER.info(
                    "Trade exit logged: trades.id=%d pnl=$%.2f reason=%s",
                    trade_id, position.realized_pnl_dollars, position.exit_reason,
                )
                return True
        except Exception as exc:
            LOGGER.error("Failed to log trade exit (trade_id=%d): %s", trade_id, exc)
            return False

    def log_equity_snapshot(
        self,
        strategy_id: int,
        equity: float,
        daily_pnl: float,
        daily_trades: int,
        note: str = "",
    ) -> bool:
        """Write an end-of-day equity snapshot."""
        conn = self._get_conn()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO equity_snapshots (
                        strategy_id, snapshot_date, equity,
                        daily_pnl, daily_trades, note, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (strategy_id, snapshot_date)
                    DO UPDATE SET
                        equity       = EXCLUDED.equity,
                        daily_pnl    = EXCLUDED.daily_pnl,
                        daily_trades = EXCLUDED.daily_trades,
                        note         = EXCLUDED.note
                    """,
                    (
                        strategy_id,
                        datetime.now(EASTERN).date(),
                        equity, daily_pnl, daily_trades, note,
                    ),
                )
                LOGGER.info(
                    "Equity snapshot logged: $%.2f (daily_pnl=$%.2f, trades=%d)",
                    equity, daily_pnl, daily_trades,
                )
                return True
        except Exception as exc:
            LOGGER.error("Failed to log equity snapshot: %s", exc)
            return False

    def log_portfolio_event(
        self,
        event_type: str,
        details: dict,
        strategy_id: Optional[int] = None,
    ) -> bool:
        """Log a notable event (halt, watchdog trigger, error) to portfolio_events."""
        conn = self._get_conn()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO portfolio_events (
                        strategy_id, event_type, event_time, details, created_at
                    ) VALUES (%s, %s, NOW(), %s, NOW())
                    """,
                    (strategy_id, event_type, json.dumps(details)),
                )
                return True
        except Exception as exc:
            LOGGER.error("Failed to log portfolio event %s: %s", event_type, exc)
            return False

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

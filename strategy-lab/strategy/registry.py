"""
strategy/registry.py -- PostgreSQL strategy registry for InfiniteLoop.

Owns the schema for all tables shared between Layer 1 (Strategy Lab) and
Layer 2 (Execution Agent). Call initialize_schema() at startup to ensure
the tables exist with the correct schema.

Tables owned here:
  - oracle_runs:        One row per Hermes optimization run
  - oracle_iterations:  One row per Hermes iteration within a run
  - strategies:         Validated Oracle + Trade param sets (inter-layer bus)
  - trades:             Live/paper fills written by execution-agent
  - equity_snapshots:   Daily equity written by execution-agent
  - portfolio_events:   Notable events written by execution-agent

Schema contract:
  - Layer 1 writes to: oracle_runs, oracle_iterations, strategies
  - Layer 2 reads from: strategies (status='active')
  - Layer 2 writes to: trades, equity_snapshots, portfolio_events
  - Layer 3 reads from: everything
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
from psycopg2.extras import Json

LOGGER = logging.getLogger("infiniteloop.strategy.registry")

# Default trade params used when promoting an Oracle-only strategy to active.
# Layer 2 uses these until the Trade Agent (Phase 2) optimizes them.
DEFAULT_TRADE_PARAMS = {
    "entry_minute":      45,    # enter after 45 min of RTH (10:15 AM ET)
    "short_delta":       20,    # sell the ~20-delta strike
    "spread_width":       5,    # 5-point wide spread ($500 max loss per contract)
    "profit_target_pct": 50,    # close at 50% of max credit received
    "stop_loss_pct":    200,    # close if loss = 2x the credit received
    "forced_exit_hour":  15,    # 3:00 PM ET hard close (watchdog enforces 3:45)
    "condor_em_mult":   1.0,    # iron condor strikes at 1x expected move
    "em_mult_high":     1.0,    # sell short strike at 1.0x expected move (directional)
    "em_mult_low":      0.5,    # buy long strike at 0.5x expected move from short
}


class StrategyRegistry:
    """Read/write the strategy registry in PostgreSQL."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")

    @contextmanager
    def _connect(self) -> Iterator[psycopg2.extensions.connection]:
        connection = psycopg2.connect(self.database_url)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        """
        Create all tables if they don't exist.

        Detects the old schema (strategies.params column) and migrates by
        dropping and recreating the four execution tables. oracle_runs and
        oracle_iterations are never dropped -- they hold Hermes history.
        """
        with self._connect() as conn:
            cur = conn.cursor()

            # -- Oracle history tables (never dropped) --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oracle_runs (
                    id               SERIAL PRIMARY KEY,
                    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at      TIMESTAMPTZ,
                    seed_params      JSONB NOT NULL,
                    best_params      JSONB,
                    best_accuracy    REAL,
                    total_iterations INTEGER DEFAULT 0,
                    notes            TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oracle_iterations (
                    id                SERIAL PRIMARY KEY,
                    run_id            INTEGER NOT NULL REFERENCES oracle_runs(id) ON DELETE CASCADE,
                    iteration         INTEGER NOT NULL,
                    param_changed     TEXT NOT NULL,
                    old_value         REAL,
                    new_value         REAL,
                    overall_accuracy  REAL NOT NULL,
                    up_accuracy       REAL,
                    down_accuracy     REAL,
                    neutral_accuracy  REAL,
                    skip_rate         REAL,
                    accepted          BOOLEAN NOT NULL,
                    hermes_reasoning  TEXT,
                    full_params       JSONB NOT NULL,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_oracle_iterations_run_id
                ON oracle_iterations (run_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_oracle_iterations_param
                ON oracle_iterations (param_changed, overall_accuracy)
            """)

            # -- Webull token persistence (one row, upserted on each successful auth) --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webull_tokens (
                    id         INTEGER PRIMARY KEY DEFAULT 1,
                    token      TEXT NOT NULL,
                    expires    BIGINT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'NORMAL',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # -- Detect stale schema: old schema has 'params', new has 'oracle_params' --
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'strategies'
            """)
            existing_cols = {row[0] for row in cur.fetchall()}

            if existing_cols and "oracle_params" not in existing_cols:
                LOGGER.warning(
                    "Old strategies schema detected -- migrating "
                    "(dropping strategies, trades, equity_snapshots, portfolio_events)"
                )
                cur.execute("DROP TABLE IF EXISTS portfolio_events CASCADE")
                cur.execute("DROP TABLE IF EXISTS equity_snapshots CASCADE")
                cur.execute("DROP TABLE IF EXISTS trades CASCADE")
                cur.execute("DROP TABLE IF EXISTS strategies CASCADE")
                existing_cols = set()

            # -- strategies: inter-layer bus (Layer 1 writes, Layer 2 reads) --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id                    SERIAL PRIMARY KEY,
                    name                  TEXT NOT NULL,
                    oracle_params         JSONB NOT NULL,
                    trade_params          JSONB,
                    overall_accuracy      REAL,
                    directional_precision REAL,
                    notes                 TEXT,
                    status                TEXT NOT NULL DEFAULT 'candidate',
                    promoted_at           TIMESTAMPTZ,
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # -- trades: written by execution-agent/trade_logging/trade_logger.py --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id                 SERIAL PRIMARY KEY,
                    strategy_id        INTEGER REFERENCES strategies(id),
                    order_id           TEXT,
                    trade_date         DATE NOT NULL,
                    direction          TEXT NOT NULL,
                    spread_type        TEXT NOT NULL,
                    spread_width       REAL NOT NULL,
                    contracts          INTEGER NOT NULL DEFAULT 1,
                    entry_credit_pts   REAL,
                    entry_credit_usd   REAL,
                    max_loss_usd       REAL,
                    spx_price_at_entry REAL,
                    vix_at_entry       REAL,
                    expected_move      REAL,
                    oracle_confidence  REAL,
                    exit_debit_pts     REAL,
                    exit_debit_usd     REAL,
                    realized_pnl_usd   REAL,
                    exit_reason        TEXT,
                    exit_time          TIMESTAMPTZ,
                    equity_after       REAL,
                    is_paper           BOOLEAN NOT NULL DEFAULT TRUE,
                    status             TEXT NOT NULL DEFAULT 'OPEN',
                    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_strategy_date
                ON trades (strategy_id, trade_date DESC)
            """)

            # -- equity_snapshots: daily EOD equity --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id            SERIAL PRIMARY KEY,
                    strategy_id   INTEGER REFERENCES strategies(id),
                    snapshot_date DATE NOT NULL,
                    equity        REAL NOT NULL,
                    daily_pnl     REAL,
                    daily_trades  INTEGER DEFAULT 0,
                    note          TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(strategy_id, snapshot_date)
                )
            """)

            # -- portfolio_events: halts, watchdog triggers, errors --
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_events (
                    id          SERIAL PRIMARY KEY,
                    strategy_id INTEGER REFERENCES strategies(id),
                    event_type  TEXT NOT NULL,
                    event_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    details     JSONB,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

        LOGGER.info("Schema initialized")

    # -------------------------------------------------------------------------
    # Layer 1 write helpers
    # -------------------------------------------------------------------------

    def save_oracle_candidate(
        self,
        name: str,
        oracle_params: dict,
        results,
        notes: str = "",
        status: str = "candidate",
    ) -> int:
        """
        Save a validated Oracle param set to the strategies table.

        Args:
            name:          Unique name (e.g. 'oracle_v3_run7')
            oracle_params: Direction threshold params dict
            results:       oracle.backtest.OracleResults instance
            notes:         Free-text notes
            status:        'candidate' by default; use promote_strategy() to activate

        Returns:
            strategies.id of the saved row
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO strategies
                    (name, oracle_params, overall_accuracy, directional_precision, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    name,
                    Json(oracle_params),
                    results.overall_accuracy,
                    results.directional_precision,
                    notes,
                    status,
                ),
            )
            row_id = int(cur.fetchone()[0])
            LOGGER.info(
                "Saved Oracle candidate '%s' -> strategies.id=%d "
                "(accuracy=%.2f%% dir_prec=%.2f%%)",
                name, row_id,
                results.overall_accuracy * 100,
                results.directional_precision * 100,
            )
            return row_id

    def promote_strategy(
        self,
        strategy_id: int,
        trade_params: Optional[dict] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Promote a strategy to 'active' status so Layer 2 picks it up.

        Retires any currently active strategy first.
        Sets promoted_at = NOW().

        Args:
            strategy_id:  strategies.id to promote
            trade_params: Optional Trade Agent params to attach (defaults to DEFAULT_TRADE_PARAMS)
            notes:        Optional notes to set

        Returns:
            True on success
        """
        tparams = trade_params if trade_params is not None else DEFAULT_TRADE_PARAMS

        with self._connect() as conn:
            cur = conn.cursor()

            # Retire any currently active strategy
            cur.execute("UPDATE strategies SET status = 'retired' WHERE status = 'active'")

            # Build UPDATE
            set_parts = ["status = 'active'", "promoted_at = NOW()", "trade_params = %s"]
            params: list = [Json(tparams)]

            if notes is not None:
                set_parts.append("notes = %s")
                params.append(notes)

            params.append(strategy_id)
            cur.execute(
                "UPDATE strategies SET " + ", ".join(set_parts) + " WHERE id = %s RETURNING id",
                params,
            )
            row = cur.fetchone()
            if row is None:
                LOGGER.error("promote_strategy: strategies.id=%d not found", strategy_id)
                return False

            LOGGER.info("Promoted strategies.id=%d to 'active'", strategy_id)
            return True

    def get_active_strategy(self) -> Optional[dict]:
        """Return the currently active strategy row, or None."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, oracle_params, trade_params,
                       overall_accuracy, directional_precision, notes, promoted_at
                FROM strategies
                WHERE status = 'active'
                ORDER BY promoted_at DESC NULLS LAST
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "id":                    row[0],
                "name":                  row[1],
                "oracle_params":         row[2],
                "trade_params":          row[3],
                "overall_accuracy":      row[4],
                "directional_precision": row[5],
                "notes":                 row[6],
                "promoted_at":           row[7].isoformat() if row[7] else None,
            }

    def list_candidates(self, limit: int = 20) -> list[dict]:
        """Return the most recent strategy candidates, newest first."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, overall_accuracy, directional_precision, status, created_at
                FROM strategies
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [
                {
                    "id":                    r[0],
                    "name":                  r[1],
                    "overall_accuracy":      r[2],
                    "directional_precision": r[3],
                    "status":                r[4],
                    "created_at":            r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]

    def get_best_oracle_params(self) -> Optional[dict]:
        """
        Return the oracle_params from the highest-precision non-retired candidate,
        or None if no candidates exist.
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT oracle_params, overall_accuracy, directional_precision, id, name
                FROM strategies
                WHERE status != 'retired'
                ORDER BY directional_precision DESC NULLS LAST,
                         overall_accuracy DESC NULLS LAST
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "oracle_params":         row[0],
                "overall_accuracy":      row[1],
                "directional_precision": row[2],
                "id":                    row[3],
                "name":                  row[4],
            }

    # -------------------------------------------------------------------------
    # Backward-compatibility alias
    # -------------------------------------------------------------------------

    def save_strategy(
        self,
        name: str,
        oracle_params: dict,
        results,
        notes: str = "",
        status: str = "candidate",
    ) -> int:
        """Alias for save_oracle_candidate() -- kept for compatibility with loop.py."""
        return self.save_oracle_candidate(
            name=name,
            oracle_params=oracle_params,
            results=results,
            notes=notes,
            status=status,
        )

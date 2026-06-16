"""PostgreSQL registry for InfiniteLoop Phase 1 strategies and results."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg2
from psycopg2.extras import Json

from backtest.metrics import StrategyScorecard

LOGGER = logging.getLogger("infiniteloop.strategy.registry")


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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS oracle_runs (
                    id SERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    seed_params JSONB NOT NULL,
                    best_params JSONB,
                    best_accuracy REAL,
                    total_iterations INTEGER DEFAULT 0,
                    notes TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS oracle_iterations (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES oracle_runs(id) ON DELETE CASCADE,
                    iteration INTEGER NOT NULL,
                    param_changed TEXT NOT NULL,
                    old_value REAL,
                    new_value REAL,
                    overall_accuracy REAL NOT NULL,
                    up_accuracy REAL,
                    down_accuracy REAL,
                    neutral_accuracy REAL,
                    skip_rate REAL,
                    accepted BOOLEAN NOT NULL,
                    hermes_reasoning TEXT,
                    full_params JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_oracle_iterations_run_id
                ON oracle_iterations (run_id)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_oracle_iterations_param
                ON oracle_iterations (param_changed, overall_accuracy)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    description TEXT,
                    params JSONB NOT NULL,
                    scorecard JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    validated_at TIMESTAMPTZ,
                    UNIQUE(name, version)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    strategy_id INTEGER REFERENCES strategies(id),
                    symbol TEXT NOT NULL,
                    trade_type TEXT NOT NULL,
                    direction_signal TEXT NOT NULL,
                    direction_correct BOOLEAN,
                    short_strike REAL,
                    long_strike REAL,
                    call_short_strike REAL,
                    call_long_strike REAL,
                    expiry_date DATE NOT NULL,
                    credit_received REAL,
                    spread_width REAL,
                    contracts INTEGER NOT NULL DEFAULT 1,
                    entry_time TIMESTAMPTZ,
                    exit_time TIMESTAMPTZ,
                    entry_spread_price REAL,
                    exit_spread_price REAL,
                    pnl_dollars REAL,
                    exit_reason TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id SERIAL PRIMARY KEY,
                    snapshot_date DATE NOT NULL UNIQUE,
                    equity REAL NOT NULL,
                    daily_pnl REAL,
                    trades_today INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_events (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    description TEXT,
                    metadata JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )

    def save_strategy(self, name: str, version: int, description: str, params: dict, scorecard: StrategyScorecard, status: str = "candidate") -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strategies (name, version, description, params, scorecard, status, validated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name, version) DO UPDATE
                SET description = EXCLUDED.description,
                    params = EXCLUDED.params,
                    scorecard = EXCLUDED.scorecard,
                    status = EXCLUDED.status,
                    validated_at = EXCLUDED.validated_at
                RETURNING id
                """,
                (name, version, description, Json(params), Json(scorecard.to_dict()), status, datetime.utcnow()),
            )
            return int(cursor.fetchone()[0])

    def get_active_strategy(self) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, name, version, description, params, scorecard, status FROM strategies WHERE status = 'active' ORDER BY validated_at DESC NULLS LAST, created_at DESC LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "name": row[1],
                "version": row[2],
                "description": row[3],
                "params": row[4],
                "scorecard": row[5],
                "status": row[6],
            }

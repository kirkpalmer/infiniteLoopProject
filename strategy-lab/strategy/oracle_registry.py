"""
strategy/oracle_registry.py — Persistent storage for Oracle Hermes optimization runs.

Every iteration is written immediately after evaluation. This gives Hermes
cross-session memory: it sees every parameter it has ever tried, what accuracy
it produced, and whether it was accepted. This prevents:
  - Repeating rejected parameter values
  - Ignoring previously-discovered good configurations
  - Losing all history when a run crashes or times out

Backends (same interface, chosen by open_oracle_registry()):
  OracleRegistry        — PostgreSQL (primary; the inter-layer bus)
  SqliteOracleRegistry  — local SQLite fallback at strategy-lab/data/oracle_history.db,
                          used ONLY when Postgres is unreachable so that history
                          is never silently lost. The dashboard health endpoint
                          reports which backend is active.

Usage:
    backend, registry = open_oracle_registry()
    run_id = registry.create_run(seed_params, notes="...")
    registry.save_iteration(run_id=..., iteration=..., ...)
    registry.finish_run(run_id, best_params, best_accuracy, total_iterations)
    history = registry.load_cross_session_history(limit=50)
    best = registry.get_best_params_ever()
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

LOGGER = logging.getLogger("infiniteloop.strategy.oracle_registry")

# Default location of the SQLite fallback DB (strategy-lab/data/)
SQLITE_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "oracle_history.db"


def _iso(value) -> str | None:
    """Render a timestamp (datetime from PG, str from SQLite) as ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_dict(value) -> dict | None:
    """JSONB comes back as dict from PG; SQLite stores JSON text."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return json.loads(value)


class BaseOracleRegistry(ABC):
    """Shared row-mapping logic; subclasses provide connections + SQL dialect."""

    backend: str = "abstract"

    # -- dialect hooks ------------------------------------------------------
    @abstractmethod
    def _connect(self):
        """Context manager yielding a DB-API connection (committed on exit)."""

    @abstractmethod
    def _ph(self) -> str:
        """Parameter placeholder for this dialect ('%s' or '?')."""

    @abstractmethod
    def _json(self, obj: dict):
        """Wrap a dict for insertion into a JSON column."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create oracle_runs / oracle_iterations if missing (idempotent)."""

    # -- health -------------------------------------------------------------
    def ping(self) -> bool:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True

    def counts(self) -> dict:
        """Quick stats for the health endpoint."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM oracle_runs")
            runs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM oracle_iterations")
            iters = cur.fetchone()[0]
        return {"runs": int(runs), "iterations": int(iters)}

    # -- run lifecycle --------------------------------------------------------
    def create_run(self, seed_params: dict, notes: str = "") -> int:
        ph = self._ph()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO oracle_runs (seed_params, notes) VALUES ({ph}, {ph})",
                (self._json(seed_params), notes),
            )
            run_id = self._last_insert_id(cur, "oracle_runs")
        LOGGER.info("OracleRegistry[%s]: created run %d", self.backend, run_id)
        return run_id

    def finish_run(
        self, run_id: int, best_params: dict, best_accuracy: float, total_iterations: int
    ) -> None:
        ph = self._ph()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE oracle_runs
                SET finished_at = {ph}, best_params = {ph},
                    best_accuracy = {ph}, total_iterations = {ph}
                WHERE id = {ph}
                """,
                (
                    datetime.utcnow().isoformat() if self.backend == "sqlite" else datetime.utcnow(),
                    self._json(best_params),
                    float(best_accuracy),
                    int(total_iterations),
                    run_id,
                ),
            )
        LOGGER.info(
            "OracleRegistry[%s]: finished run %d | best_accuracy=%.2f%% | iterations=%d",
            self.backend, run_id, best_accuracy * 100, total_iterations,
        )

    # -- iteration persistence ------------------------------------------------
    def save_iteration(
        self,
        run_id: int,
        iteration: int,
        param_changed: str,
        old_value: float | None,
        new_value: float,
        overall_accuracy: float,
        up_accuracy: float,
        down_accuracy: float,
        neutral_accuracy: float,
        skip_rate: float,
        accepted: bool,
        hermes_reasoning: str,
        full_params: dict,
    ) -> None:
        ph = self._ph()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                INSERT INTO oracle_iterations (
                    run_id, iteration, param_changed, old_value, new_value,
                    overall_accuracy, up_accuracy, down_accuracy, neutral_accuracy,
                    skip_rate, accepted, hermes_reasoning, full_params
                ) VALUES ({", ".join([ph] * 13)})
                """,
                (
                    run_id, iteration, param_changed,
                    float(old_value) if old_value is not None else None,
                    float(new_value),
                    float(overall_accuracy),
                    float(up_accuracy),
                    float(down_accuracy),
                    float(neutral_accuracy),
                    float(skip_rate),
                    accepted,
                    hermes_reasoning,
                    self._json(full_params),
                ),
            )

    # -- history loading --------------------------------------------------------
    def load_cross_session_history(self, limit: int = 80) -> list[dict]:
        """Most recent `limit` iterations across ALL runs, oldest first."""
        ph = self._ph()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT run_id, iteration, param_changed, old_value, new_value,
                       overall_accuracy, up_accuracy, down_accuracy, neutral_accuracy,
                       skip_rate, accepted, hermes_reasoning, full_params, created_at, id
                FROM oracle_iterations
                ORDER BY id DESC
                LIMIT {ph}
                """,
                (limit,),
            )
            rows = cur.fetchall()

        result = []
        for row in reversed(rows):  # chronological order
            result.append({
                "run_id": row[0],
                "iteration": row[1],
                "param_changed": row[2],
                "old_value": row[3],
                "new_value": row[4],
                "overall_accuracy": round(row[5], 4) if row[5] is not None else None,
                "up_accuracy": round(row[6], 4) if row[6] is not None else None,
                "down_accuracy": round(row[7], 4) if row[7] is not None else None,
                "neutral_accuracy": round(row[8], 4) if row[8] is not None else None,
                "skip_rate": round(row[9], 4) if row[9] is not None else None,
                "accepted": bool(row[10]),
                "hermes_reasoning": row[11],
                "full_params": _as_dict(row[12]),
                "timestamp": _iso(row[13]),
                "db_id": row[14],
            })
        return result

    def get_best_params_ever(self) -> dict | None:
        """full_params of the highest-accuracy ACCEPTED iteration across all runs."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT full_params, overall_accuracy
                FROM oracle_iterations
                WHERE accepted = {self._true_literal()}
                ORDER BY overall_accuracy DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
        if row is None:
            return None
        LOGGER.info(
            "OracleRegistry[%s]: best ever params found with accuracy=%.2f%%",
            self.backend, row[1] * 100,
        )
        return _as_dict(row[0])

    def get_all_completed_trials(self, min_accuracy: float = 0.40) -> list[tuple[dict, float]]:
        """
        Return (params_dict, overall_accuracy) for every iteration that has
        full_params and passed the accuracy floor. Used to warm-start Optuna
        with all historical knowledge so it doesn't re-explore known regions.
        Results ordered best-first.
        """
        ph = self._ph()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT full_params, overall_accuracy
                FROM oracle_iterations
                WHERE full_params IS NOT NULL
                  AND overall_accuracy >= {ph}
                ORDER BY overall_accuracy DESC
                """,
                (min_accuracy,),
            )
            rows = cur.fetchall()
        result = []
        for params_json, acc in rows:
            params = _as_dict(params_json)
            if params and acc is not None:
                result.append((params, float(acc)))
        LOGGER.info(
            "OracleRegistry[%s]: loaded %d completed trials (min_accuracy=%.2f)",
            self.backend, len(result), min_accuracy,
        )
        return result

    def get_param_landscape(self) -> list[dict]:
        """All iterations grouped by param for the dashboard scatter graph."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT param_changed, new_value, overall_accuracy, accepted, created_at
                FROM oracle_iterations
                ORDER BY param_changed, new_value
                """
            )
            rows = cur.fetchall()
        return [
            {
                "param": row[0],
                "value": row[1],
                "accuracy": round(row[2], 4) if row[2] is not None else None,
                "accepted": bool(row[3]),
                "timestamp": _iso(row[4]),
            }
            for row in rows
        ]

    def get_tried_values(self) -> dict[str, list[dict]]:
        """
        param_name -> [{value, accuracy, accepted}] for EVERY value ever tried.
        Injected into the Hermes prompt so it never re-proposes a known value
        and can see which direction each parameter responded to.
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT param_changed, new_value, overall_accuracy, accepted
                FROM oracle_iterations
                ORDER BY param_changed, new_value
                """
            )
            rows = cur.fetchall()
        tried: dict[str, list[dict]] = {}
        seen: set[tuple] = set()
        for param, value, acc, accepted in rows:
            key = (param, value)
            if key in seen:
                continue
            seen.add(key)
            tried.setdefault(param, []).append({
                "value": value,
                "accuracy": round(acc, 4) if acc is not None else None,
                "accepted": bool(accepted),
            })
        return tried

    def get_rejected_values(self) -> dict[str, list]:
        """param_name -> [values] that were tried and rejected across all runs."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT param_changed, new_value
                FROM oracle_iterations
                WHERE accepted = {self._false_literal()}
                ORDER BY param_changed, new_value
                """
            )
            rows = cur.fetchall()
        rejected: dict[str, list] = {}
        for param, value in rows:
            rejected.setdefault(param, [])
            if value not in rejected[param]:
                rejected[param].append(value)
        return rejected

    def get_run_summary(self) -> list[dict]:
        """One row per oracle_run with key stats. Used in dashboard."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    r.id, r.started_at, r.finished_at, r.best_accuracy,
                    r.total_iterations, r.notes,
                    COUNT(i.id) FILTER (WHERE i.accepted) AS accepted_count
                FROM oracle_runs r
                LEFT JOIN oracle_iterations i ON i.run_id = r.id
                GROUP BY r.id, r.started_at, r.finished_at, r.best_accuracy,
                         r.total_iterations, r.notes
                ORDER BY r.id DESC
                """
            )
            rows = cur.fetchall()
        return [
            {
                "run_id": row[0],
                "started_at": _iso(row[1]),
                "finished_at": _iso(row[2]),
                "best_accuracy": round(row[3], 4) if row[3] is not None else None,
                "total_iterations": row[4],
                "notes": row[5],
                "accepted_count": row[6],
            }
            for row in rows
        ]

    # -- dialect helpers ------------------------------------------------------
    def _true_literal(self) -> str:
        return "TRUE"

    def _false_literal(self) -> str:
        return "FALSE"

    def _last_insert_id(self, cur, table: str) -> int:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PostgreSQL backend (primary)
# ---------------------------------------------------------------------------

class OracleRegistry(BaseOracleRegistry):
    """PostgreSQL-backed registry (the inter-layer bus)."""

    backend = "postgres"

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise ValueError("DATABASE_URL is required — set it in .env or the environment")

    @contextmanager
    def _connect(self):
        import psycopg2
        connection = psycopg2.connect(self.database_url, connect_timeout=10)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ph(self) -> str:
        return "%s"

    def _json(self, obj: dict):
        from psycopg2.extras import Json
        return Json(obj)

    def _last_insert_id(self, cur, table: str) -> int:
        cur.execute(f"SELECT currval(pg_get_serial_sequence('{table}', 'id'))")
        return int(cur.fetchone()[0])

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
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
            cur.execute(
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
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_oracle_iterations_run_id ON oracle_iterations (run_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_oracle_iterations_param "
                "ON oracle_iterations (param_changed, overall_accuracy)"
            )


# ---------------------------------------------------------------------------
# SQLite fallback backend
# ---------------------------------------------------------------------------

class SqliteOracleRegistry(BaseOracleRegistry):
    """
    Local fallback so Oracle history is NEVER silently lost when Postgres is
    down. Same interface as OracleRegistry. Data lives in
    strategy-lab/data/oracle_history.db.
    """

    backend = "sqlite"

    def __init__(self, db_path: Path | str = SQLITE_FALLBACK_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ph(self) -> str:
        return "?"

    def _json(self, obj: dict):
        return json.dumps(obj)

    def _true_literal(self) -> str:
        return "1"

    def _false_literal(self) -> str:
        return "0"

    def _last_insert_id(self, cur, table: str) -> int:
        return int(cur.lastrowid)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS oracle_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    seed_params TEXT NOT NULL,
                    best_params TEXT,
                    best_accuracy REAL,
                    total_iterations INTEGER DEFAULT 0,
                    notes TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS oracle_iterations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    accepted INTEGER NOT NULL,
                    hermes_reasoning TEXT,
                    full_params TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_oracle_iterations_run_id ON oracle_iterations (run_id)"
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def open_oracle_registry(
    database_url: str | None = None,
    sqlite_path: Path | str = SQLITE_FALLBACK_PATH,
) -> tuple[str, BaseOracleRegistry]:
    """
    Open the best available registry backend.

    Returns ("postgres", registry) when Postgres is reachable, otherwise
    ("sqlite", registry). Never returns None — Oracle history must always
    be persisted somewhere.
    """
    url = database_url or os.getenv("DATABASE_URL")
    if url:
        try:
            registry = OracleRegistry(url)
            registry.ensure_schema()
            registry.ping()
            LOGGER.info("OracleRegistry: PostgreSQL backend connected")
            return "postgres", registry
        except Exception as exc:
            LOGGER.warning(
                "OracleRegistry: PostgreSQL unreachable (%s) — falling back to SQLite at %s",
                exc, sqlite_path,
            )
    else:
        LOGGER.warning(
            "OracleRegistry: DATABASE_URL not set — falling back to SQLite at %s", sqlite_path
        )

    registry = SqliteOracleRegistry(sqlite_path)
    registry.ensure_schema()
    return "sqlite", registry

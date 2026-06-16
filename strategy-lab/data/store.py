"""Local SQLite cache for SPX/SPY OHLCV, VIX data, day features, and
synthetic pricing inputs. Avoids repeat yfinance/FRED network calls on every backtest run."""

from __future__ import annotations

import logging
import pickle
import sqlite3
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("infiniteloop.data.store")


class DataStore:
    """SQLite cache for local Phase 1 data artifacts."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS futures_cache (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    tf TEXT NOT NULL,
                    data BLOB NOT NULL,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, date, tf)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS options_cache (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    data BLOB NOT NULL,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS features_cache (
                    symbol TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    data BLOB NOT NULL,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, start_date, end_date)
                )
                """
            )

    def _save_frame(self, table: str, columns: tuple[str, ...], values: tuple[str, ...], df: pd.DataFrame) -> None:
        blob = pickle.dumps(df, protocol=pickle.HIGHEST_PROTOCOL)
        column_clause = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO {table} ({column_clause}, data) VALUES ({placeholders}, ?)"
        with self._connect() as connection:
            connection.execute(sql, (*values, blob))

    def _load_frame(self, table: str, where_clause: str, parameters: tuple[str, ...]) -> pd.DataFrame | None:
        sql = f"SELECT data FROM {table} WHERE {where_clause}"
        with self._connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            return None
        return pickle.loads(row[0])

    def save_futures(self, symbol: str, date: str, tf: str, df: pd.DataFrame) -> None:
        self._save_frame("futures_cache", ("symbol", "date", "tf"), (symbol, date, tf), df)

    def load_futures(self, symbol: str, date: str, tf: str) -> pd.DataFrame | None:
        return self._load_frame("futures_cache", "symbol = ? AND date = ? AND tf = ?", (symbol, date, tf))

    def save_options(self, symbol: str, date: str, df: pd.DataFrame) -> None:
        self._save_frame("options_cache", ("symbol", "date"), (symbol, date), df)

    def load_options(self, symbol: str, date: str) -> pd.DataFrame | None:
        return self._load_frame("options_cache", "symbol = ? AND date = ?", (symbol, date))

    def save_features(self, symbol: str, start_date: str, end_date: str, df: pd.DataFrame) -> None:
        self._save_frame("features_cache", ("symbol", "start_date", "end_date"), (symbol, start_date, end_date), df)

    def load_features(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self._load_frame("features_cache", "symbol = ? AND start_date = ? AND end_date = ?", (symbol, start_date, end_date))

"""Market data orchestrator for InfiniteLoop Strategy Lab.
Routes data requests to the right source: Barchart CSVs (already on disk) for
SPX daily and ES 1-min windows, yfinance for SPY hourly on all other dates,
FRED for the risk-free rate. Caches via DataStore when available."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from .barchart_csv import get_es_covered_dates, load_all_es_1min, load_spx_daily, load_vix_history
from .firstrate import covered_dates as firstrate_covered_dates
from .firstrate import firstrate_available, load_firstrate_es

LOGGER = logging.getLogger("infiniteloop.data.market_data")

class MarketDataClient:
    """Load and serve Phase 1 market data from local files and free sources."""

    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self.spx_daily = load_spx_daily()
        self.vix = load_vix_history()
        self.es_1min: pd.DataFrame | None = None
        # ES intraday source: FirstRateData continuous (preferred — full
        # history, US/Eastern index) with Barchart CSVs as the fallback.
        if firstrate_available():
            self.es_source = "firstrate"
            self.es_1min = load_firstrate_es()
            self.es_covered_dates = firstrate_covered_dates(self.es_1min)
        else:
            self.es_source = "barchart"
            self.es_covered_dates = get_es_covered_dates()
        self._spy_hourly_cache: dict[str, pd.DataFrame] = {}
        self._risk_free_rate_cache: tuple[datetime, float] | None = None
        self.enable_spy_fallback = os.getenv("ENABLE_SPY_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}

    def get_spx_daily(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Return the SPX daily slice for the requested range."""

        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        frame = self.spx_daily.loc[(self.spx_daily.index >= start) & (self.spx_daily.index <= end)].copy()
        expected_dates = pd.date_range(start.normalize(), end.normalize(), freq="B")
        missing_dates = expected_dates.difference(frame.index)
        if not missing_dates.empty:
            LOGGER.warning("SPX daily data has %d missing business dates in range %s-%s", len(missing_dates), start_date, end_date)
        return frame

    def _ensure_es_1min_loaded(self) -> pd.DataFrame:
        if self.es_1min is None:
            self.es_1min = load_all_es_1min()
        return self.es_1min

    def _fetch_spy_hourly(self, target_date: date) -> pd.DataFrame:
        cache_key = target_date.isoformat()
        if cache_key in self._spy_hourly_cache:
            return self._spy_hourly_cache[cache_key]

        import yfinance as yf

        start = (target_date - timedelta(days=1)).isoformat()
        end = (target_date + timedelta(days=1)).isoformat()
        frame = yf.download(
            "SPY",
            start=start,
            end=end,
            interval="1h",
            auto_adjust=False,
            prepost=True,
            progress=False,
            threads=False,
        )
        if frame.empty:
            LOGGER.warning("No SPY hourly data returned for %s", cache_key)
            result = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            self._spy_hourly_cache[cache_key] = result
            return result

        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.droplevel(0)
        frame = frame.rename(columns=str.lower)
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        frame.index = frame.index.tz_convert("US/Eastern")
        frame = frame[[column for column in ["open", "high", "low", "close", "volume"] if column in frame.columns]].copy()
        self._spy_hourly_cache[cache_key] = frame
        return frame

    def get_intraday_bars(self, date: date, bar_minutes: int = 60) -> pd.DataFrame:
        """Return intraday bars for a given date from the correct source."""

        if date in self.es_covered_dates:
            es_frame = self._ensure_es_1min_loaded()
            # Reach back 5 calendar days so the previous TRADING day is always
            # included (Mondays need Friday; Tuesdays after Monday holidays
            # need the prior Friday). _session_parts picks the latest prior
            # day from whatever is in the window. Fetching from "yesterday"
            # silently dropped every Monday from the feature set.
            session_start = pd.Timestamp.combine(date - timedelta(days=5), pd.Timestamp.min.time()).tz_localize("US/Eastern") + pd.Timedelta(hours=9, minutes=30)
            session_end = pd.Timestamp.combine(date, pd.Timestamp.min.time()).tz_localize("US/Eastern") + pd.Timedelta(hours=16)
            if self.es_source == "firstrate":
                # Index is already US/Eastern and sorted — binary-search slice
                # (a full-index tz_convert per call would cost ~50ms x 1600 days)
                frame = es_frame.loc[session_start:session_end].copy()
                source = "ES_FIRSTRATE"
            else:
                eastern_index = es_frame.index.tz_convert("US/Eastern")
                mask = (eastern_index >= session_start) & (eastern_index <= session_end)
                frame = es_frame.loc[mask].copy()
                frame.index = eastern_index[mask]
                source = "ES_CSV"
            if bar_minutes > 1 and not frame.empty:
                frame = (
                    frame.resample(f"{bar_minutes}min")
                    .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                    .dropna(subset=["open", "high", "low", "close"])
                )
        else:
            if not self.enable_spy_fallback:
                frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                source = "NO_FALLBACK"
            else:
                frame = self._fetch_spy_hourly(date)
                source = "SPY_HOURLY"
                if bar_minutes > 1 and not frame.empty:
                    frame = (
                        frame.resample(f"{bar_minutes}min")
                        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                        .dropna(subset=["open", "high", "low", "close"])
                    )

        LOGGER.debug("Loaded intraday bars for %s from %s (%d rows)", date.isoformat(), source, len(frame))
        return frame

    def get_vix_on(self, date: date) -> float | None:
        """Return the VIX close for a date if available."""

        timestamp = pd.Timestamp(date)
        if timestamp in self.vix.index:
            return float(self.vix.loc[timestamp, "vix_close"])
        return None

    def get_risk_free_rate(self, as_of_date: str | None = None) -> float:
        """Return the 3-month Treasury rate as a decimal."""

        now = datetime.utcnow()
        if self._risk_free_rate_cache is not None:
            cached_at, cached_rate = self._risk_free_rate_cache
            if now - cached_at < timedelta(hours=24):
                return cached_rate

        try:
            from pandas_datareader import data as web

            end = pd.Timestamp(as_of_date) if as_of_date is not None else pd.Timestamp.utcnow().normalize()
            start = end - pd.Timedelta(days=14)
            series = web.DataReader("DTB3", "fred", start, end).dropna()
            if series.empty:
                raise RuntimeError("FRED returned no DTB3 data")
            rate = float(series.iloc[-1, 0]) / 100.0
        except Exception as exc:  # pragma: no cover - network fallback
            LOGGER.warning("FRED DTB3 unavailable, using fallback rate 0.05: %s", exc)
            rate = 0.05

        self._risk_free_rate_cache = (now, rate)
        return rate

    def coverage_report(self) -> str:
        """Return a readable summary of local data coverage."""

        spx_start = self.spx_daily.index.min().date().isoformat()
        spx_end = self.spx_daily.index.max().date().isoformat()
        vix_start = self.vix.index.min().date().isoformat()
        vix_end = self.vix.index.max().date().isoformat()
        return (
            f"SPX daily: {spx_start} to {spx_end} ({len(self.spx_daily)} rows)\n"
            f"ES 1-min ({self.es_source}): {len(self.es_covered_dates)} trading days covered\n"
            f"VIX: {vix_start} to {vix_end} ({len(self.vix)} rows)"
        )

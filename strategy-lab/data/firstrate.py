"""
data/firstrate.py — Loader for FirstRateData ES 1-minute continuous history.

File format (no header, timestamps in US/Eastern, ratio-adjusted continuous):
    2008-01-02 06:00:00,1478.96,1480.46,1478.71,1480.21,2317

The raw file is ~6.5M rows / 360MB, so parsing happens ONCE and the result is
cached as a pickle next to the source file. The cache is invalidated when the
source file's mtime changes (e.g. after a data update download).

Why ratio-adjusted: all cross-day features (gap_pct, overnight_range_pct,
prev-close comparisons) are percentages, which ratio adjustment preserves
across contract rolls. Within-day math and the trade simulator's SPX scaling
are invariant to the per-day constant factor. See SESSION_NOTES_2026_06_10.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("infiniteloop.data.firstrate")

FIRSTRATE_DIR = Path(__file__).resolve().parent / "raw" / "firstrate"
CACHE_NAME = "es_1min_cache.pkl"

# Keep ~6 months of pre-window context so the first usable feature day has
# prev-day / overnight history available.
DEFAULT_START = "2017-06-01"

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _source_file(directory: Path = FIRSTRATE_DIR) -> Path | None:
    """The FirstRateData continuous file (largest .txt/.csv in the folder)."""
    if not directory.exists():
        return None
    candidates = [
        p for p in list(directory.glob("*.txt")) + list(directory.glob("*.csv"))
        if "cache" not in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def firstrate_available(directory: Path = FIRSTRATE_DIR) -> bool:
    return _source_file(directory) is not None


def load_firstrate_es(
    path: Path | None = None,
    start: str = DEFAULT_START,
    directory: Path = FIRSTRATE_DIR,
) -> pd.DataFrame:
    """
    Load the FirstRateData ES 1-min continuous series (US/Eastern index,
    columns: open/high/low/close/volume), from cache when possible.
    """
    source = path or _source_file(directory)
    if source is None:
        raise FileNotFoundError(f"No FirstRateData file found in {directory}")

    cache_path = source.parent / CACHE_NAME
    if cache_path.exists() and cache_path.stat().st_mtime >= source.stat().st_mtime:
        frame = pd.read_pickle(cache_path)
        LOGGER.info(
            "FirstRate ES loaded from cache: %d rows (%s -> %s)",
            len(frame), frame.index.min(), frame.index.max(),
        )
        return frame.loc[frame.index >= pd.Timestamp(start, tz="US/Eastern")]

    LOGGER.info("Parsing FirstRate ES source %s (first run — this takes a minute)...", source.name)
    frame = pd.read_csv(source, names=COLUMNS, header=None)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    frame = frame.dropna(subset=["timestamp"])

    # Trim BEFORE tz-localizing — cheaper, and keeps the cache small.
    frame = frame.loc[frame["timestamp"] >= pd.Timestamp(start)].copy()

    # FirstRateData timestamps are US/Eastern wall-clock. DST edge cases:
    # ambiguous (fall-back hour) and nonexistent (spring-forward hour) bars
    # are dropped — at most one overnight hour twice a year.
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(
        "US/Eastern", ambiguous="NaT", nonexistent="NaT"
    )
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.set_index("timestamp").sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    frame = frame[["open", "high", "low", "close", "volume"]]

    try:
        frame.to_pickle(cache_path)
        LOGGER.info("FirstRate ES cache written: %s (%d rows)", cache_path.name, len(frame))
    except OSError as exc:
        LOGGER.warning("Could not write FirstRate cache (continuing without): %s", exc)

    LOGGER.info(
        "FirstRate ES parsed: %d rows (%s -> %s)",
        len(frame), frame.index.min(), frame.index.max(),
    )
    return frame


def covered_dates(frame: pd.DataFrame) -> set:
    """Trading dates (US/Eastern) present in the frame — used by the feature
    loader to decide which days have intraday data."""
    return set(pd.Series(frame.index.date).unique())

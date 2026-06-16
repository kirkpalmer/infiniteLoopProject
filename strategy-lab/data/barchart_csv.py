"""Loaders for Barchart.com CSV exports already downloaded to
strategy-lab/data/raw/. Handles the Barchart column naming convention (Latest=close),
quoted timestamps, Central Time localization, and footer stripping."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("infiniteloop.data.barchart_csv")
RAW_DIR = Path(__file__).resolve().parent / "raw"


def _strip_footer(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    first_value = str(frame.iloc[-1, 0])
    if first_value.startswith("Downloaded"):
        return frame.iloc[:-1].copy()
    return frame


def _pick_first_csv(directory: Path) -> Path:
    candidates = sorted(directory.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No CSV files found in {directory}")
    return candidates[0]


def _load_one_spx_daily(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame = _strip_footer(frame)
    frame = frame.rename(
        columns={
            "Time": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Latest": "close",
            "Volume": "volume",
        }
    )
    frame = frame.drop(columns=[column for column in ["Change", "%Change"] if column in frame.columns])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    frame = frame.set_index("date").sort_index()
    LOGGER.info("Loaded SPX daily CSV %s (%d rows)", path.name, len(frame))
    return frame[["open", "high", "low", "close", "volume"]]


def load_spx_daily(csv_path: Path | None = None) -> pd.DataFrame:
    """Load and merge ALL SPX daily CSVs in data/raw/spx_daily/.

    Multiple files are supported so coverage gaps can be backfilled from other
    sources (e.g. the 2018-2019 yfinance backfill) without touching the
    original Barchart download. Duplicate dates: first file alphabetically
    wins (the values agree — it's the same index)."""

    if csv_path is not None:
        return _load_one_spx_daily(csv_path)

    directory = RAW_DIR / "spx_daily"
    paths = sorted(directory.glob("*.csv"))
    if not paths:
        raise FileNotFoundError(f"No CSV files found in {directory}")
    frames = [_load_one_spx_daily(path) for path in paths]
    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="first")]
    LOGGER.info(
        "SPX daily merged: %d rows from %d files (%s -> %s)",
        len(merged), len(frames), merged.index.min().date(), merged.index.max().date(),
    )
    return merged


def load_es_1min_csv(csv_path: Path) -> pd.DataFrame:
    """Load one ES futures 1-minute Barchart CSV export."""

    frame = pd.read_csv(csv_path)
    frame = _strip_footer(frame)
    frame = frame.rename(
        columns={
            "Time": "timestamp",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Latest": "close",
            "Volume": "volume",
        }
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype(str).str.strip('"'), format="%Y-%m-%d %H:%M", errors="coerce")
    frame["timestamp"] = frame["timestamp"].dt.tz_localize("US/Central")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.loc[frame["volume"].fillna(0) != 0].copy()
    frame = frame.set_index("timestamp").sort_index()
    LOGGER.info("Loaded ES 1-min CSV %s (%d rows)", csv_path.name, len(frame))
    return frame[["open", "high", "low", "close", "volume"]]


def load_all_es_1min(futures_dir: Path | None = None) -> pd.DataFrame:
    """Load all ES 1-minute CSV exports in the futures directory."""

    directory = futures_dir or (RAW_DIR / "futures")
    frames = [load_es_1min_csv(path) for path in sorted(directory.glob("es[hmuz]??_*.csv"))]
    if not frames:
        raise FileNotFoundError(f"No ES futures CSV files found in {directory}")
    frame = pd.concat(frames).sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    LOGGER.info("Loaded %d ES 1-min rows from %d files", len(frame), len(frames))
    return frame


def get_es_covered_dates(futures_dir: Path | None = None) -> set[pd.Timestamp]:
    """Return the set of trading dates covered by the ES CSV files."""

    directory = futures_dir or (RAW_DIR / "futures")
    covered_dates: set[pd.Timestamp] = set()
    for path in sorted(directory.glob("es[hmuz]??_*.csv")):
        frame = load_es_1min_csv(path)
        covered_dates.update(ts.tz_convert("US/Eastern").date() for ts in frame.index)
    return covered_dates


def load_vix_history(csv_path: Path | None = None) -> pd.DataFrame:
    """Load the downloaded VIX daily history CSV."""

    path = csv_path or _pick_first_csv(RAW_DIR / "vix")
    frame = pd.read_csv(path)
    frame = _strip_footer(frame)
    frame["DATE"] = pd.to_datetime(frame["DATE"], format="%m/%d/%Y", errors="coerce").dt.tz_localize(None)
    frame = frame.rename(columns={"CLOSE": "vix_close"})
    frame = frame.dropna(subset=["DATE", "vix_close"])
    frame = frame.set_index("DATE").sort_index()
    LOGGER.info("Loaded VIX history CSV %s (%d rows)", path.name, len(frame))
    return frame[["vix_close"]]

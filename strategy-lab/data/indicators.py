"""ES order flow indicators for InfiniteLoop direction classification.
Computes ORB, gap, delta proxy, VWAP, and session-boundary features from 1-min bars."""

from __future__ import annotations

import logging
from datetime import time

import pandas as pd

LOGGER = logging.getLogger("infiniteloop.data.indicators")


def _ensure_eastern(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if result.index.tz is None:
        result.index = result.index.tz_localize("US/Eastern")
    else:
        result.index = result.index.tz_convert("US/Eastern")
    return result


def delta_proxy(frame: pd.DataFrame) -> pd.Series:
    """Approximate order-flow delta from OHLCV bars."""

    data = frame.copy()
    price_range = (data["high"] - data["low"]).abs() + 1e-9
    bullish = (data["close"] >= data["open"]) * ((data["close"] - data["low"]) / price_range) * data["volume"]
    bearish = (data["close"] < data["open"]) * -((data["high"] - data["close"]) / price_range) * data["volume"]
    return (bullish + bearish).rename("delta")


def cumulative_delta(frame: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling sum of delta proxy over the provided window."""

    return delta_proxy(frame).rolling(window=window, min_periods=1).sum().rename("cum_delta")


def vwap_daily(frame: pd.DataFrame) -> pd.Series:
    """VWAP that resets at the start of each trading day."""

    data = _ensure_eastern(frame)
    typical_price = (data["high"] + data["low"] + data["close"]) / 3.0
    grouped_dates = data.index.normalize()
    numerator = (typical_price * data["volume"]).groupby(grouped_dates).cumsum()
    denominator = data["volume"].groupby(grouped_dates).cumsum().replace(0, pd.NA)
    return (numerator / denominator).rename("vwap")


def opening_range(frame: pd.DataFrame, n_minutes: int = 30) -> tuple[pd.Series, pd.Series]:
    """Compute the opening high and low for each trading day."""

    data = _ensure_eastern(frame)
    daily_highs: dict[pd.Timestamp, float] = {}
    daily_lows: dict[pd.Timestamp, float] = {}
    for day, day_frame in data.groupby(data.index.normalize()):
        rth = day_frame.between_time("09:30", "10:00", inclusive="left")
        opening_slice = rth.head(n_minutes)
        if opening_slice.empty:
            continue
        daily_highs[day] = float(opening_slice["high"].max())
        daily_lows[day] = float(opening_slice["low"].min())
    return pd.Series(daily_highs, name="orb_high"), pd.Series(daily_lows, name="orb_low")


def relative_volume(frame: pd.DataFrame, window: int = 20) -> pd.Series:
    """Volume divided by rolling average volume."""

    volume = frame["volume"].astype(float)
    return (volume / volume.rolling(window=window, min_periods=1).mean()).rename("rel_vol")


def compute_gap_pct(prev_close: float, current_open: float) -> float:
    """Return the signed percentage gap between two prices."""

    if prev_close == 0:
        return 0.0
    return (current_open - prev_close) / prev_close

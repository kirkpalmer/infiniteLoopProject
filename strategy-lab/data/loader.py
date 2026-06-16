"""Loads SPX daily OHLCV data via MarketDataClient and assembles
per-day feature rows for the direction classification model. Each row represents
one trading day with morning session features as columns."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, time

import pandas as pd

from constants import ORB_CONFIRM_MINUTES, ORB_MINUTES, OOS_SPLIT
from .indicators import compute_gap_pct, cumulative_delta, opening_range, relative_volume, vwap_daily
from .market_data import MarketDataClient

LOGGER = logging.getLogger("infiniteloop.data.loader")


@dataclass
class DayFeatureRow:
    date: pd.Timestamp
    prev_range_pct: float
    prev_close_vs_vwap: float
    prev_day_return_pct: float        # prior day (close - open) / open — direction + magnitude
    prev_final_hour_delta: float
    prev_volume_ratio: float
    gap_pct: float
    overnight_range_pct: float
    open_vs_overnight_pct: float
    orb_high: float
    orb_low: float
    orb_range_pct: float
    orb_breakout: str                 # measured at the post-ORB confirmation bar, NOT the daily close
    post_orb_close: float             # price ~ORB_CONFIRM_MINUTES after the ORB window closes
    delta_bias_first30: float
    vwap_at_30: float
    vwap_slope_first30: float         # relative VWAP slope across the first 30 min
    rel_volume_first30: float
    day_outcome: str


def _as_et(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if data.index.tz is None:
        data.index = data.index.tz_localize("US/Eastern")
    else:
        data.index = data.index.tz_convert("US/Eastern")
    return data


def _session_parts(frame: pd.DataFrame, trading_date: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Split a multi-day intraday window into (prev_rth, overnight, current_rth).

    The previous day must be the latest prior day that actually has RTH bars —
    for a Monday the calendar-previous day is Sunday, which only has Globex
    evening bars; naively picking it produced an empty prev_rth and silently
    dropped EVERY Monday from the feature set. The overnight session is
    everything between the previous day's 16:00 close and today's 09:30 open,
    which correctly includes the Sunday Globex session for Mondays.
    """
    data = _as_et(frame)
    current_day = trading_date.date()
    date_values = pd.Index(data.index.date)
    rth_mask = (data.index.time >= pd.Timestamp("09:30").time()) & (data.index.time < pd.Timestamp("16:00").time())

    # Latest prior day that has RTH bars (skips weekend Globex-only "days")
    previous_day = current_day
    for day in sorted({d for d in date_values if d < current_day}, reverse=True):
        if bool(((date_values == day) & rth_mask).any()):
            previous_day = day
            break

    prev_rth = data.loc[(date_values == previous_day) & rth_mask]
    current_rth = data.loc[(date_values == current_day) & rth_mask]

    prev_close_ts = pd.Timestamp.combine(previous_day, pd.Timestamp("16:00").time()).tz_localize("US/Eastern")
    cur_open_ts = pd.Timestamp.combine(current_day, pd.Timestamp("09:30").time()).tz_localize("US/Eastern")
    overnight = data.loc[(data.index >= prev_close_ts) & (data.index < cur_open_ts)]

    return prev_rth, overnight, current_rth


def load_day_features(
    client: MarketDataClient,
    start_date: str,
    end_date: str,
    orb_minutes: int = ORB_MINUTES,
) -> pd.DataFrame:
    """Load day-level features for the direction model."""

    spx_daily = client.get_spx_daily(start_date, end_date)
    rows: list[DayFeatureRow] = []
    es_days = 0
    spy_days = 0

    for trading_date in spx_daily.index[1:]:
        intraday = client.get_intraday_bars(trading_date.date(), bar_minutes=1)
        if intraday.empty:
            continue
        if trading_date.date() in client.es_covered_dates:
            es_days += 1
        else:
            spy_days += 1

        prev_rth, overnight, current_rth = _session_parts(intraday, trading_date)
        if prev_rth.empty or current_rth.empty:
            continue

        prev_day = spx_daily.loc[spx_daily.index < trading_date].iloc[-1]
        prev_day_key = pd.Timestamp(prev_day.name).normalize()
        prev_day_return_pct = (float(prev_day["close"]) - float(prev_day["open"])) / max(float(prev_day["open"]), 1e-9)
        prev_vwap = vwap_daily(prev_rth).iloc[-1]
        prev_delta = cumulative_delta(prev_rth, window=20)
        prev_volume_average = float(spx_daily["volume"].rolling(20, min_periods=1).mean().loc[prev_day_key])
        prev_volume_ratio = prev_rth["volume"].sum() / max(prev_volume_average, 1.0)

        current_open = float(current_rth.iloc[0]["open"])
        current_close = float(current_rth.iloc[-1]["close"])
        prev_close = float(prev_day["close"])
        gap_pct = compute_gap_pct(prev_close, current_open)

        overnight_high = float(overnight["high"].max()) if not overnight.empty else current_open
        overnight_low = float(overnight["low"].min()) if not overnight.empty else current_open
        overnight_range_pct = (overnight_high - overnight_low) / max(prev_close, 1e-9)
        open_vs_overnight_pct = (current_open - overnight_low) / max(overnight_high - overnight_low, 1e-9)

        or_high, or_low = opening_range(current_rth, n_minutes=orb_minutes)
        orb_key = pd.Timestamp(current_rth.index[0].date())
        orb_high = float(or_high.get(orb_key, current_open))
        orb_low = float(or_low.get(orb_key, current_open))
        orb_range_pct = (orb_high - orb_low) / max(orb_low, 1e-9)

        first_30 = current_rth.head(orb_minutes)
        delta_bias_first30 = float(cumulative_delta(first_30, window=min(20, len(first_30))).iloc[-1]) if not first_30.empty else 0.0
        vwap_first30 = vwap_daily(first_30) if not first_30.empty else None
        vwap_at_30 = float(vwap_first30.iloc[-1]) if vwap_first30 is not None else current_open
        if vwap_first30 is not None and len(vwap_first30) >= 4:
            vwap_mid = float(vwap_first30.iloc[len(vwap_first30) // 2])
            vwap_slope_first30 = (vwap_at_30 - vwap_mid) / max(abs(vwap_mid), 1e-9)
        else:
            vwap_slope_first30 = 0.0
        rel_volume_first30 = float(relative_volume(first_30, window=min(20, len(first_30))).iloc[-1]) if not first_30.empty else 0.0

        # day_outcome is the LABEL — it legitimately uses the daily close.
        if current_close >= current_open * 1.003:
            day_outcome = "UP"
        elif current_close <= current_open * 0.997:
            day_outcome = "DOWN"
        else:
            day_outcome = "NEUTRAL"

        # ORB breakout is a FEATURE — it must only use information available
        # at classification time (~minute 45). Using the daily close here was
        # lookahead bias: it leaked the day's outcome into the feature set and
        # inflated every backtest accuracy number.
        confirm_bars = min(orb_minutes + ORB_CONFIRM_MINUTES, len(current_rth))
        post_orb_close = float(current_rth.iloc[confirm_bars - 1]["close"])
        orb_breakout = "none"
        if post_orb_close > orb_high:
            orb_breakout = "up"
        elif post_orb_close < orb_low:
            orb_breakout = "down"

        rows.append(
            DayFeatureRow(
                date=trading_date,
                prev_range_pct=(float(prev_day["high"]) - float(prev_day["low"])) / max(float(prev_day["close"]), 1e-9),
                prev_close_vs_vwap=(float(prev_day["close"]) - prev_vwap) / max(prev_vwap, 1e-9),
                prev_day_return_pct=prev_day_return_pct,
                prev_final_hour_delta=float(prev_delta.tail(60).sum()) if not prev_delta.empty else 0.0,
                prev_volume_ratio=float(prev_volume_ratio),
                gap_pct=float(gap_pct),
                overnight_range_pct=float(overnight_range_pct),
                open_vs_overnight_pct=float(open_vs_overnight_pct),
                orb_high=orb_high,
                orb_low=orb_low,
                orb_range_pct=float(orb_range_pct),
                orb_breakout=orb_breakout,
                post_orb_close=post_orb_close,
                delta_bias_first30=delta_bias_first30,
                vwap_at_30=vwap_at_30,
                vwap_slope_first30=vwap_slope_first30,
                rel_volume_first30=rel_volume_first30,
                day_outcome=day_outcome,
            )
        )

    frame = pd.DataFrame([asdict(row) for row in rows]).set_index("date")
    outcome_counts = frame["day_outcome"].value_counts().to_dict() if not frame.empty else {}
    LOGGER.info("Loaded %d feature rows (%d ES days, %d SPY days); outcomes=%s", len(frame), es_days, spy_days, outcome_counts)
    return frame


def split_train_oos(df: pd.DataFrame, oos_split: float = OOS_SPLIT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split features into train and out-of-sample sets."""

    split_index = int(len(df) * oos_split)
    return df.iloc[:split_index].copy(), df.iloc[split_index:].copy()

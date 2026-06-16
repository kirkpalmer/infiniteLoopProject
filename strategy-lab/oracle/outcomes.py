"""
oracle/outcomes.py — Direction labeler for Oracle training data.

Labels each historical SPX trading day as UP, DOWN, or NEUTRAL based on
the direction of the close relative to the open — NOT magnitude.

  UP      -> close > open + flat_band  (market went up any meaningful amount)
  DOWN    -> close < open - flat_band  (market went down any meaningful amount)
  NEUTRAL -> |close - open| <= flat_band  (genuinely flat, no clear direction)

The expected move (EM = SPX_open x VIX/100 x sqrt(1/252)) is still computed
and stored in the output frame — Sigma uses it for strike placement. But EM
is NOT used to classify the day's direction. A day that closes above the open
is an UP day for Oracle regardless of whether it exceeded the expected move.

This means Oracle's accuracy metric is honest: if Oracle called UP and the
market closed above the open, Oracle was right and a bull put spread would
have profited (assuming strikes were placed below the close).
"""

from __future__ import annotations

import logging
from math import sqrt

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("infiniteloop.oracle.outcomes")

TRADING_DAYS_PER_YEAR = 252

# Days closing within this fraction of the open are labeled NEUTRAL.
# 0.05% = ~2.5 SPX points at 5000 — essentially flat.
FLAT_THRESHOLD_PCT = 0.0005


def compute_expected_move(spx_open: float, vix_close: float) -> float:
    """Return the 1-sigma expected daily move in SPX points (used by Sigma for strikes)."""
    return spx_open * (vix_close / 100.0) * sqrt(1.0 / TRADING_DAYS_PER_YEAR)


def label_outcomes(
    spx_daily: pd.DataFrame,
    vix: pd.DataFrame,
) -> pd.DataFrame:
    """
    Label each SPX trading day as UP, DOWN, or NEUTRAL.

    Labeling is direction-based (close vs open), not magnitude-based.
    EM is computed and stored for Sigma but does NOT determine the label.

    Args:
        spx_daily: SPX daily OHLCV DataFrame with 'open' and 'close' columns.
        vix:       DataFrame indexed by date with column 'vix_close'.

    Returns:
        DataFrame with columns:
            outcome          — 'UP', 'DOWN', or 'NEUTRAL'
            vix_close        — VIX closing level for that day
            expected_move    — 1-sigma EM in SPX points (for Sigma strike placement)
            expected_move_pct — EM as fraction of open price
    """
    df = spx_daily[["open", "close"]].copy()

    # Align VIX (forward-fill over weekends/holidays)
    vix_aligned = vix[["vix_close"]].reindex(df.index, method="ffill")
    df["vix_close"] = vix_aligned["vix_close"]

    # Expected move — stored for Sigma, not used for labeling
    df["expected_move"] = (
        df["open"].astype(float)
        * (df["vix_close"].astype(float) / 100.0)
        * (1.0 / TRADING_DAYS_PER_YEAR) ** 0.5
    )
    df["expected_move_pct"] = df["expected_move"] / df["open"].replace(0, float("nan"))

    spx_open  = df["open"].astype(float)
    spx_close = df["close"].astype(float)

    # Direction-based labeling — flat band around open, not EM
    flat_band = spx_open * FLAT_THRESHOLD_PCT
    df["outcome"] = np.select(
        [spx_close > spx_open + flat_band, spx_close < spx_open - flat_band],
        ["UP", "DOWN"],
        default="NEUTRAL",
    )

    # Drop rows where VIX was unavailable (can't compute EM for Sigma)
    before = len(df)
    df = df.dropna(subset=["expected_move"])
    dropped = before - len(df)
    if dropped:
        LOGGER.warning("Dropped %d rows with missing VIX data", dropped)

    counts = df["outcome"].value_counts().to_dict()
    total  = len(df)
    LOGGER.info(
        "Labeled %d days -> UP: %d (%.1f%%), DOWN: %d (%.1f%%), NEUTRAL: %d (%.1f%%)",
        total,
        counts.get("UP", 0),      100 * counts.get("UP", 0)      / max(total, 1),
        counts.get("DOWN", 0),    100 * counts.get("DOWN", 0)    / max(total, 1),
        counts.get("NEUTRAL", 0), 100 * counts.get("NEUTRAL", 0) / max(total, 1),
    )
    return df[["outcome", "vix_close", "expected_move", "expected_move_pct"]]


def merge_outcomes_into_features(
    features: pd.DataFrame,
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    """
    Join Oracle outcome labels onto the features DataFrame.

    Adds: outcome, vix_close, expected_move columns.
    The day_outcome column from loader.py is intentionally replaced by the
    direction-based outcome column computed here.
    """
    result = features.join(
        outcomes[["outcome", "vix_close", "expected_move"]], how="inner"
    )
    dropped = len(features) - len(result)
    if dropped:
        LOGGER.warning("Dropped %d feature rows without outcome labels", dropped)
    return result

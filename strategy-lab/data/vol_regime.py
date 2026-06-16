"""Volatility regime gate for InfiniteLoop. Provides per-day VIX and
VIX-rank proxy data. Used as a pre-classifier gate: only trade when the vol regime is favorable
for premium selling (IV rich enough, VIX not extreme)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from constants import DEFAULT_MAX_VIX, DEFAULT_MIN_IV_RANK_PCT
from .barchart_csv import load_vix_history as _load_vix_csv

LOGGER = logging.getLogger("infiniteloop.data.vol_regime")


def load_vix_history(vix_csv_path: str | None = None) -> pd.DataFrame:
    """Load VIX daily closes from the manually downloaded CBOE CSV."""

    if vix_csv_path is None:
        return _load_vix_csv()
    return _load_vix_csv(Path(vix_csv_path))


@dataclass
class VolRegimeData:
    """Compute a rolling VIX percentile proxy for the premium-selling gate."""

    vix_df: pd.DataFrame

    def __post_init__(self) -> None:
        frame = self.vix_df.copy()
        if frame.index.tz is not None:
            frame.index = frame.index.tz_localize(None)
        if "vix_close" not in frame.columns:
            raise ValueError("vix_df must contain a 'vix_close' column")
        self.vix_df = frame.sort_index()

    def get_regime(self, value: pd.Timestamp) -> dict[str, float] | None:
        """Return the VIX and rolling percentile proxy for a date."""

        timestamp = pd.Timestamp(value).normalize()
        if timestamp not in self.vix_df.index:
            LOGGER.warning("No VIX data available for %s", timestamp.date())
            return None

        window = self.vix_df.loc[:timestamp].tail(252)
        vix_value = float(self.vix_df.loc[timestamp, "vix_close"])
        rank_pct = float(window["vix_close"].rank(pct=True).iloc[-1] * 100.0)
        return {"vix": vix_value, "vix_rank_pct": rank_pct}

    def is_favorable(
        self,
        value: pd.Timestamp,
        min_vix_rank: float = DEFAULT_MIN_IV_RANK_PCT,
        max_vix: float = DEFAULT_MAX_VIX,
    ) -> tuple[bool, str]:
        """Return whether the regime is favorable for premium selling."""

        regime = self.get_regime(value)
        if regime is None:
            return False, "no_data"
        if regime["vix_rank_pct"] < min_vix_rank:
            return False, "low_vix_rank"
        if regime["vix"] > max_vix:
            return False, "high_vix"
        return True, ""

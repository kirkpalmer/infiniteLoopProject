"""ORB Direction Strategy - the seed 0DTE strategy for InfiniteLoop.
Uses the Opening Range Breakout (first 30 min of RTH) as the primary directional signal,
filtered by overnight gap and ES delta bias."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from constants import DEFAULT_SHORT_DELTA, DEFAULT_SPREAD_WIDTH_USD, DEFAULT_PROFIT_TARGET_PCT, DEFAULT_STOP_LOSS_PCT, GAP_THRESHOLD_PCT, NEUTRAL_BAND_PCT, ORB_BREAKOUT_PCT, ORB_MINUTES
from .base import BaseStrategy


@dataclass
class ORBDirectionParams:
    gap_threshold_pct: float = GAP_THRESHOLD_PCT
    orb_breakout_pct: float = ORB_BREAKOUT_PCT
    delta_bias_threshold: float = 150.0
    neutral_band_pct: float = NEUTRAL_BAND_PCT
    entry_hour: int = 10
    short_delta: int = DEFAULT_SHORT_DELTA
    spread_width_usd: int = DEFAULT_SPREAD_WIDTH_USD
    profit_target_pct: int = DEFAULT_PROFIT_TARGET_PCT
    stop_loss_pct: int = DEFAULT_STOP_LOSS_PCT
    forced_exit_hour: int = 15


class ORBDirectionStrategy(BaseStrategy):
    def __init__(self, params: ORBDirectionParams = ORBDirectionParams()) -> None:
        self.params = params

    def get_name(self) -> str:
        return "orb_direction"

    def get_params(self) -> dict:
        return asdict(self.params)

    def get_spread_params(self) -> dict:
        return {
            "entry_hour": self.params.entry_hour,
            "short_delta": self.params.short_delta,
            "spread_width_usd": self.params.spread_width_usd,
            "profit_target_pct": self.params.profit_target_pct,
            "stop_loss_pct": self.params.stop_loss_pct,
            "forced_exit_hour": self.params.forced_exit_hour,
        }

    def classify_direction(self, data: pd.DataFrame | pd.Series) -> str:
        row = data.iloc[0] if isinstance(data, pd.DataFrame) else data
        required = ["gap_pct", "orb_breakout", "delta_bias_first30", "open_vs_overnight_pct", "orb_high", "orb_low", "orb_range_pct"]
        if any(pd.isna(row.get(field)) for field in required):
            return "SKIP"

        gap_pct = float(row["gap_pct"])
        delta_bias = float(row["delta_bias_first30"])
        orb_breakout = str(row["orb_breakout"])

        if abs(gap_pct) > self.params.gap_threshold_pct:
            gap_direction = "UP" if gap_pct > 0 else "DOWN"
            if (gap_direction == "UP" and orb_breakout == "up") or (gap_direction == "DOWN" and orb_breakout == "down"):
                return gap_direction
            return "NEUTRAL"

        if abs(gap_pct) <= self.params.neutral_band_pct:
            if abs(delta_bias) > self.params.delta_bias_threshold:
                return "UP" if delta_bias > 0 else "DOWN"
            return "NEUTRAL"

        if orb_breakout == "up" and delta_bias > 0:
            return "UP"
        if orb_breakout == "down" and delta_bias < 0:
            return "DOWN"
        return "NEUTRAL"

    @classmethod
    def from_params(cls, params_dict: dict) -> "ORBDirectionStrategy":
        return cls(ORBDirectionParams(**params_dict))

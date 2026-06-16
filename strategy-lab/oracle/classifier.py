"""
oracle/classifier.py — Oracle direction classifier.

Returns OracleSignal: direction + confidence + per-class scores.
Sigma uses confidence and lean to decide trade structure.

Features used (4 signals, equally weighted in score):
  1. gap_pct              — overnight gap direction/magnitude
  2. orb_breakout         — opening range breakout direction
  3. delta_bias_first30   — first 30-min order-flow delta
  4. prior-day context    — prev_day_return_pct + prev_close_vs_vwap

The prior-day signal was added to improve DOWN accuracy: DOWN days
frequently have weak opening signals but show bearish continuation
from a prior day that closed below VWAP with a negative return.
"""
from __future__ import annotations
import logging
from dataclasses import asdict, dataclass, field
from typing import ClassVar
import pandas as pd

LOGGER = logging.getLogger("infiniteloop.oracle.classifier")

ORACLE_OPTIMIZABLE_PARAMS: frozenset[str] = frozenset({
    "gap_threshold_pct", "orb_breakout_pct", "delta_bias_threshold",
    "neutral_band_pct", "vwap_slope_threshold", "vol_filter_high", "vol_filter_low",
    "prev_day_return_threshold", "prev_day_vwap_threshold",
    "min_confidence", "min_score_separation",
})
ORACLE_FROZEN_PARAMS: frozenset[str] = frozenset({"agent_name", "version"})


@dataclass
class OracleSignal:
    direction:     str    # UP / DOWN / NEUTRAL / SKIP
    confidence:    float  # winner score / total signal energy
    up_score:      float  # 0-1 how strongly features support UP
    down_score:    float  # 0-1 how strongly features support DOWN
    neutral_score: float  # 0-1 how strongly features support NEUTRAL
    skip_reason:   str = ""   # why a SKIP happened: missing_features / vix_above_high / vix_below_low

    @property
    def lean(self) -> str:
        if self.direction != "NEUTRAL":
            return "NONE"
        if abs(self.up_score - self.down_score) < 0.08:
            return "NONE"
        return "UP" if self.up_score > self.down_score else "DOWN"

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "up_score": round(self.up_score, 4),
            "down_score": round(self.down_score, 4),
            "neutral_score": round(self.neutral_score, 4),
            "lean": self.lean,
            "skip_reason": self.skip_reason,
        }


@dataclass
class OracleParams:
    # Opening session signals
    gap_threshold_pct:    float = 0.0025
    neutral_band_pct:     float = 0.0020
    orb_breakout_pct:     float = 0.0012
    delta_bias_threshold: float = 150.0
    vwap_slope_threshold: float = 0.00005
    # VIX regime filter
    vol_filter_high:      float = 32.0
    vol_filter_low:       float = 12.0
    # Prior-day context signals (new — improves DOWN accuracy)
    # A prior day with |return| > threshold and close vs VWAP beyond threshold
    # is used as a tiebreaker when opening-session signals are ambiguous.
    prev_day_return_threshold: float = 0.003   # 0.3% — e.g. prior day moved >0.3%
    prev_day_vwap_threshold:   float = 0.001   # 0.1% — prior day close vs VWAP
    # Conviction gates (added after OOS showed conf<0.5 calls hit 7.7% accuracy)
    # If a directional call's up/down scores are separated by less than
    # min_score_separation, the day is reclassified NEUTRAL — no conviction
    # means no coin-flip directional call (tests Kirk's "ambiguous = sideways
    # day" hypothesis). If a remaining UP/DOWN call's confidence is below
    # min_confidence, the day is SKIPPED (reason: low_confidence).
    min_confidence:        float = 0.0
    min_score_separation:  float = 0.0
    agent_name: str = field(default="oracle", init=False)
    version:    int = field(default=1, init=False)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OracleParams":
        return cls(**{k: v for k, v in d.items() if k in ORACLE_OPTIMIZABLE_PARAMS})


class OracleStrategy:
    REQUIRED_FEATURES: ClassVar[list[str]] = [
        "gap_pct", "orb_breakout", "delta_bias_first30",
        "orb_high", "orb_low", "orb_range_pct",
    ]
    # Prior-day features are optional — classifier degrades gracefully if absent
    PRIOR_DAY_FEATURES: ClassVar[list[str]] = [
        "prev_day_return_pct", "prev_close_vs_vwap",
    ]

    def __init__(self, params: OracleParams | None = None) -> None:
        self.params = params or OracleParams()

    def _orb_direction(self, row: pd.Series) -> str:
        """
        ORB breakout direction with a tunable margin (orb_breakout_pct).

        Uses post_orb_close — the price ~15 min AFTER the ORB window closes,
        so no lookahead — and requires the move to clear the ORB level by
        orb_breakout_pct before counting as a breakout. Falls back to the
        precomputed orb_breakout string for feature frames that lack
        post_orb_close (built before June 2026).
        """
        post_close = row.get("post_orb_close")
        orb_high = row.get("orb_high")
        orb_low = row.get("orb_low")
        if pd.notna(post_close) and pd.notna(orb_high) and pd.notna(orb_low):
            margin = max(self.params.orb_breakout_pct, 0.0)
            if float(post_close) > float(orb_high) * (1.0 + margin):
                return "up"
            if float(post_close) < float(orb_low) * (1.0 - margin):
                return "down"
            return "none"
        return str(row.get("orb_breakout", "none"))

    def _vwap_slope(self, row: pd.Series) -> float:
        value = row.get("vwap_slope_first30")
        return float(value) if pd.notna(value) else 0.0

    def _compute_scores(self, row: pd.Series) -> tuple[float, float, float]:
        """
        Compute (up_score, down_score, neutral_score) in [0,1] from 4 signal groups.

        Signal groups (equally weighted):
          1. gap_pct            — overnight gap
          2. orb_breakout       — opening range direction
          3. delta_bias_first30 — first 30-min order flow
          4. prior-day          — prev_day_return_pct + prev_close_vs_vwap

        If prior-day features are absent, group 4 contributes 0.5 (neutral)
        and the first 3 groups are renormalized.
        """
        gap_pct      = float(row.get("gap_pct", 0.0))
        delta_bias   = float(row.get("delta_bias_first30", 0.0))
        orb_breakout = self._orb_direction(row)
        prev_return  = float(row.get("prev_day_return_pct", 0.0))
        prev_vwap    = float(row.get("prev_close_vs_vwap", 0.0))

        gap_thr      = max(self.params.gap_threshold_pct, 1e-9)
        delta_thr    = max(self.params.delta_bias_threshold, 1e-9)
        prev_ret_thr = max(self.params.prev_day_return_threshold, 1e-9)
        prev_vwap_thr= max(self.params.prev_day_vwap_threshold, 1e-9)

        # Groups 1-3: opening session
        g_up  = min(max(gap_pct, 0) / gap_thr, 2) / 2
        g_dn  = min(max(-gap_pct, 0) / gap_thr, 2) / 2
        g_neu = min(max(1 - abs(gap_pct) / gap_thr, 0), 1)

        o_up  = 1.0 if orb_breakout == "up"   else 0.0
        o_dn  = 1.0 if orb_breakout == "down" else 0.0
        o_neu = 1.0 if orb_breakout == "none" else 0.0

        d_up  = min(max(delta_bias,  0) / delta_thr, 2) / 2
        d_dn  = min(max(-delta_bias, 0) / delta_thr, 2) / 2
        d_neu = min(max(1 - abs(delta_bias) / delta_thr, 0), 1)

        # Group 4: prior-day context (continuation signal)
        # Both sub-signals must agree to produce a strong prior-day signal.
        # Average of return signal and VWAP-deviation signal.
        p_up  = (min(max(prev_return,  0) / prev_ret_thr,  2) / 2
               + min(max(prev_vwap,    0) / prev_vwap_thr, 2) / 2) / 2
        p_dn  = (min(max(-prev_return, 0) / prev_ret_thr,  2) / 2
               + min(max(-prev_vwap,   0) / prev_vwap_thr, 2) / 2) / 2
        p_neu = max(0.0, 1.0 - p_up - p_dn)

        # Equal-weight average across 4 groups
        up_score   = (g_up  + o_up  + d_up  + p_up)  / 4.0
        down_score = (g_dn  + o_dn  + d_dn  + p_dn)  / 4.0
        neu_score  = (g_neu + o_neu + d_neu + p_neu)  / 4.0
        return up_score, down_score, neu_score

    def classify(self, row: pd.Series) -> OracleSignal:
        if any(pd.isna(row.get(f)) for f in self.REQUIRED_FEATURES):
            return OracleSignal("SKIP", 0.0, 0.0, 0.0, 0.0, skip_reason="missing_features")
        vix = row.get("vix_close")
        if pd.notna(vix):
            fvix = float(vix)
            if fvix > self.params.vol_filter_high:
                return OracleSignal("SKIP", 0.0, 0.0, 0.0, 0.0, skip_reason="vix_above_high")
            if fvix < self.params.vol_filter_low:
                return OracleSignal("SKIP", 0.0, 0.0, 0.0, 0.0, skip_reason="vix_below_low")

        up_score, down_score, neu_score = self._compute_scores(row)
        direction = self._rule_direction(
            float(row["gap_pct"]),
            float(row["delta_bias_first30"]),
            self._orb_direction(row),
            float(row.get("prev_day_return_pct", 0.0)),
            float(row.get("prev_close_vs_vwap", 0.0)),
            self._vwap_slope(row),
        )
        # Conviction gate 1: a directional call with up/down scores this close
        # is a coin flip — reclassify as NEUTRAL ("no conviction = range day").
        if direction in ("UP", "DOWN") and abs(up_score - down_score) < self.params.min_score_separation:
            direction = "NEUTRAL"

        total = up_score + down_score + neu_score
        winner = up_score if direction == "UP" else (down_score if direction == "DOWN" else neu_score)
        confidence = winner / max(total, 1e-9)

        # Conviction gate 2: low-confidence DIRECTIONAL calls are skipped.
        # (OOS evidence: conf<0.5 directional calls scored 7.7% — worse than
        # chance.) NEUTRAL calls are exempt so gate 1 stays measurable.
        if direction in ("UP", "DOWN") and confidence < self.params.min_confidence:
            return OracleSignal(
                "SKIP", round(confidence, 4),
                round(up_score, 4), round(down_score, 4), round(neu_score, 4),
                skip_reason="low_confidence",
            )

        return OracleSignal(direction, round(confidence, 4), round(up_score, 4), round(down_score, 4), round(neu_score, 4))

    def _rule_direction(
        self,
        gap_pct: float,
        delta_bias: float,
        orb_breakout: str,
        prev_return: float = 0.0,
        prev_vwap_dev: float = 0.0,
        vwap_slope: float = 0.0,
    ) -> str:
        """
        Priority-based direction rule.

        Primary: gap + ORB + delta (opening session signals).
        Tiebreaker 1: prior-day return + close-vs-VWAP.
        Tiebreaker 2: first-30-min VWAP slope (vwap_slope_threshold).
        Both tiebreakers only fire when the primary signal is ambiguous.
        """
        primary = self._primary_direction(gap_pct, delta_bias, orb_breakout)

        if primary != "NEUTRAL":
            return primary

        # Opening session is ambiguous — check prior-day continuation
        ret_thr  = self.params.prev_day_return_threshold
        vwap_thr = self.params.prev_day_vwap_threshold
        prev_bearish = prev_return < -ret_thr and prev_vwap_dev < -vwap_thr
        prev_bullish = prev_return >  ret_thr and prev_vwap_dev >  vwap_thr
        if prev_bearish:
            return "DOWN"
        if prev_bullish:
            return "UP"

        # Still ambiguous — check intraday VWAP slope
        if abs(vwap_slope) > self.params.vwap_slope_threshold:
            return "UP" if vwap_slope > 0 else "DOWN"
        return "NEUTRAL"

    def _primary_direction(self, gap_pct: float, delta_bias: float, orb_breakout: str) -> str:
        """Original opening-session priority logic (unchanged)."""
        if abs(gap_pct) > self.params.gap_threshold_pct:
            gap_dir = "UP" if gap_pct > 0 else "DOWN"
            if (gap_dir == "UP"   and orb_breakout == "up") \
            or (gap_dir == "DOWN" and orb_breakout == "down"):
                return gap_dir
            if gap_dir == "UP"   and delta_bias >  self.params.delta_bias_threshold: return "UP"
            if gap_dir == "DOWN" and delta_bias < -self.params.delta_bias_threshold: return "DOWN"
            return "NEUTRAL"
        if abs(gap_pct) <= self.params.neutral_band_pct:
            if abs(delta_bias) > self.params.delta_bias_threshold:
                return "UP" if delta_bias > 0 else "DOWN"
            return "NEUTRAL"
        if orb_breakout == "up"   and delta_bias >  self.params.delta_bias_threshold: return "UP"
        if orb_breakout == "down" and delta_bias < -self.params.delta_bias_threshold: return "DOWN"
        return "NEUTRAL"

    def classify_df(self, features: pd.DataFrame) -> pd.DataFrame:
        signals = features.apply(self.classify, axis=1)
        return pd.DataFrame(
            [s.to_dict() for s in signals], index=features.index
        ).rename(columns={"direction": "predicted"})

    def get_params(self) -> dict: return self.params.to_dict()
    def get_name(self) -> str: return "oracle"

    @classmethod
    def from_params(cls, d: dict) -> "OracleStrategy":
        return cls(OracleParams.from_dict(d))

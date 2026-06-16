"""
signals/classifier.py — Apply Oracle to live RTH bars and emit a direction signal.

Workflow:
  1. On startup, load prior-day context (prior close, prior VWAP) from yfinance or DB.
  2. Each time a new 1-min bar arrives, call maybe_classify().
  3. After MIN_RTH_MINUTES_BEFORE_CLASSIFY (45) bars have accumulated, compute
     direction features and pass them to OracleStrategy.classify().
  4. Returns a ClassificationResult with direction, confidence, and skip_reason.

This module is a self-contained re-implementation of the Oracle feature extraction
logic. It does NOT import strategy-lab code — the two layers are decoupled via DB.
The OracleStrategy class is copied in (not imported) so changes to strategy-lab
don't break a running execution agent.

Feature computation from live 1-min bars:
  - gap_pct:          (today_rth_open - prior_close) / prior_close
  - orb_high/low:     high/low of first ORB_WINDOW_MINUTES bars (default 30)
  - post_orb_close:   close of bar at ORB_WINDOW_MINUTES + 15
  - delta_bias:       proxy = sum(volume if close>open else -volume if close<open else 0)
  - vwap_slope:       linear slope of running VWAP over first 30 bars
  - prev_day_return_pct: (prior_close - prior_prior_close) / prior_prior_close
  - prev_close_vs_vwap:  (prior_close - prior_vwap) / prior_vwap
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

import pytz

from constants import MIN_RTH_MINUTES_BEFORE_CLASSIFY
from data.normalizer import Bar

LOGGER = logging.getLogger("infiniteloop.signals.classifier")

EASTERN = pytz.timezone("US/Eastern")

# Opening Range Breakout window in minutes (first N RTH minutes)
ORB_WINDOW_MINUTES = 30
# Minutes after ORB close to sample post-ORB price
POST_ORB_DELAY_MINUTES = 15

# RTH start (09:30 ET)
RTH_OPEN_TIME = time(9, 30)


@dataclass
class PriorDayContext:
    """
    Prior-day data needed for gap and prior-return features.
    Populated at agent startup from yfinance or the DB.
    """
    prior_close: float          # previous RTH close (SPX or ES equivalent)
    prior_vwap: float           # previous day VWAP (or close as fallback)
    prior_prior_close: float    # two days ago close — for prev_day_return_pct
    current_vix: float          # today's VIX — for vol filter
    date: Optional[str] = None  # "YYYY-MM-DD" for logging


@dataclass
class ClassificationResult:
    """
    Output of the classifier for a single day.
    The trade agent uses direction to select the spread type.
    """
    direction: str          # UP / DOWN / NEUTRAL / SKIP
    confidence: float       # 0-1 winner score / total signal energy
    up_score: float
    down_score: float
    neutral_score: float
    lean: str               # UP / DOWN / NONE (tiebreaker inside NEUTRAL)
    skip_reason: str        # non-empty if direction == SKIP
    classified_at: Optional[datetime] = None  # ET time when signal fired

    # Raw features (for logging/debugging)
    gap_pct: float = 0.0
    orb_breakout: str = "none"
    delta_bias: float = 0.0
    vwap_slope: float = 0.0
    n_rth_bars: int = 0

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "up_score": round(self.up_score, 4),
            "down_score": round(self.down_score, 4),
            "neutral_score": round(self.neutral_score, 4),
            "lean": self.lean,
            "skip_reason": self.skip_reason,
            "classified_at": self.classified_at.isoformat() if self.classified_at else None,
            "gap_pct": round(self.gap_pct, 5),
            "orb_breakout": self.orb_breakout,
            "delta_bias": round(self.delta_bias, 1),
            "vwap_slope": self.vwap_slope,
            "n_rth_bars": self.n_rth_bars,
        }


# ── Oracle logic (self-contained copy — no import from strategy-lab) ──────────

class _OracleClassifier:
    """
    Lightweight re-implementation of OracleStrategy.classify() for live use.
    Mirrors oracle/classifier.py exactly. Update both if the scoring logic changes.
    """

    def __init__(self, params: dict) -> None:
        self.gap_threshold_pct         = float(params.get("gap_threshold_pct", 0.0025))
        self.neutral_band_pct          = float(params.get("neutral_band_pct", 0.0020))
        self.orb_breakout_pct          = float(params.get("orb_breakout_pct", 0.0012))
        self.delta_bias_threshold      = float(params.get("delta_bias_threshold", 150.0))
        self.vwap_slope_threshold      = float(params.get("vwap_slope_threshold", 5e-5))
        self.vol_filter_high           = float(params.get("vol_filter_high", 32.0))
        self.vol_filter_low            = float(params.get("vol_filter_low", 12.0))
        self.prev_day_return_threshold = float(params.get("prev_day_return_threshold", 0.003))
        self.prev_day_vwap_threshold   = float(params.get("prev_day_vwap_threshold", 0.001))
        self.min_confidence            = float(params.get("min_confidence", 0.0))
        self.min_score_separation      = float(params.get("min_score_separation", 0.0))

    def classify(
        self,
        vix: float,
        gap_pct: float,
        orb_breakout: str,     # "up" / "down" / "none"
        post_orb_close: float,
        orb_high: float,
        orb_low: float,
        delta_bias: float,
        vwap_slope: float,
        prev_day_return_pct: float,
        prev_close_vs_vwap: float,
    ) -> ClassificationResult:

        # VIX regime filter
        if not math.isnan(vix):
            if vix > self.vol_filter_high:
                return self._skip("vix_above_high", gap_pct, orb_breakout, delta_bias)
            if vix < self.vol_filter_low:
                return self._skip("vix_below_low", gap_pct, orb_breakout, delta_bias)

        # ORB direction — recompute from post_orb_close vs ORB levels
        orb_dir = orb_breakout
        if not math.isnan(post_orb_close) and not math.isnan(orb_high) and not math.isnan(orb_low):
            margin = max(self.orb_breakout_pct, 0.0)
            if post_orb_close > orb_high * (1.0 + margin):
                orb_dir = "up"
            elif post_orb_close < orb_low * (1.0 - margin):
                orb_dir = "down"
            else:
                orb_dir = "none"

        up_score, down_score, neu_score = self._compute_scores(
            gap_pct, delta_bias, orb_dir, prev_day_return_pct, prev_close_vs_vwap
        )
        direction = self._rule_direction(
            gap_pct, delta_bias, orb_dir, prev_day_return_pct, prev_close_vs_vwap, vwap_slope
        )

        # Conviction gate 1: near-tie → NEUTRAL
        if direction in ("UP", "DOWN") and abs(up_score - down_score) < self.min_score_separation:
            direction = "NEUTRAL"

        total = up_score + down_score + neu_score
        winner = up_score if direction == "UP" else (down_score if direction == "DOWN" else neu_score)
        confidence = winner / max(total, 1e-9)

        # Conviction gate 2: low-confidence directional → SKIP
        if direction in ("UP", "DOWN") and confidence < self.min_confidence:
            return self._skip("low_confidence", gap_pct, orb_dir, delta_bias,
                              confidence, up_score, down_score, neu_score)

        lean = "NONE"
        if direction == "NEUTRAL":
            if abs(up_score - down_score) >= 0.08:
                lean = "UP" if up_score > down_score else "DOWN"

        return ClassificationResult(
            direction=direction,
            confidence=round(confidence, 4),
            up_score=round(up_score, 4),
            down_score=round(down_score, 4),
            neutral_score=round(neu_score, 4),
            lean=lean,
            skip_reason="",
            gap_pct=gap_pct,
            orb_breakout=orb_dir,
            delta_bias=delta_bias,
            vwap_slope=vwap_slope,
        )

    def _skip(
        self, reason: str, gap_pct: float = 0.0, orb: str = "none", delta: float = 0.0,
        confidence: float = 0.0, up: float = 0.0, dn: float = 0.0, neu: float = 0.0,
    ) -> ClassificationResult:
        return ClassificationResult(
            direction="SKIP", confidence=confidence,
            up_score=up, down_score=dn, neutral_score=neu,
            lean="NONE", skip_reason=reason,
            gap_pct=gap_pct, orb_breakout=orb, delta_bias=delta, vwap_slope=0.0,
        )

    def _compute_scores(
        self, gap_pct: float, delta_bias: float, orb_breakout: str,
        prev_return: float, prev_vwap: float,
    ) -> tuple[float, float, float]:
        gap_thr       = max(self.gap_threshold_pct, 1e-9)
        delta_thr     = max(self.delta_bias_threshold, 1e-9)
        prev_ret_thr  = max(self.prev_day_return_threshold, 1e-9)
        prev_vwap_thr = max(self.prev_day_vwap_threshold, 1e-9)

        g_up  = min(max(gap_pct, 0)    / gap_thr,   2) / 2
        g_dn  = min(max(-gap_pct, 0)   / gap_thr,   2) / 2
        g_neu = min(max(1 - abs(gap_pct) / gap_thr, 0), 1)

        o_up  = 1.0 if orb_breakout == "up"   else 0.0
        o_dn  = 1.0 if orb_breakout == "down" else 0.0
        o_neu = 1.0 if orb_breakout == "none" else 0.0

        d_up  = min(max(delta_bias,   0) / delta_thr, 2) / 2
        d_dn  = min(max(-delta_bias,  0) / delta_thr, 2) / 2
        d_neu = min(max(1 - abs(delta_bias) / delta_thr, 0), 1)

        p_up  = (min(max(prev_return,  0) / prev_ret_thr,  2) / 2
               + min(max(prev_vwap,    0) / prev_vwap_thr, 2) / 2) / 2
        p_dn  = (min(max(-prev_return, 0) / prev_ret_thr,  2) / 2
               + min(max(-prev_vwap,   0) / prev_vwap_thr, 2) / 2) / 2
        p_neu = max(0.0, 1.0 - p_up - p_dn)

        up_score   = (g_up  + o_up  + d_up  + p_up)  / 4.0
        down_score = (g_dn  + o_dn  + d_dn  + p_dn)  / 4.0
        neu_score  = (g_neu + o_neu + d_neu + p_neu)  / 4.0
        return up_score, down_score, neu_score

    def _rule_direction(
        self, gap_pct: float, delta_bias: float, orb_breakout: str,
        prev_return: float, prev_vwap_dev: float, vwap_slope: float,
    ) -> str:
        primary = self._primary_direction(gap_pct, delta_bias, orb_breakout)
        if primary != "NEUTRAL":
            return primary
        # Prior-day tiebreaker
        ret_thr  = self.prev_day_return_threshold
        vwap_thr = self.prev_day_vwap_threshold
        if prev_return < -ret_thr and prev_vwap_dev < -vwap_thr:
            return "DOWN"
        if prev_return >  ret_thr and prev_vwap_dev >  vwap_thr:
            return "UP"
        # Intraday VWAP slope tiebreaker
        if abs(vwap_slope) > self.vwap_slope_threshold:
            return "UP" if vwap_slope > 0 else "DOWN"
        return "NEUTRAL"

    def _primary_direction(self, gap_pct: float, delta_bias: float, orb_breakout: str) -> str:
        if abs(gap_pct) > self.gap_threshold_pct:
            gap_dir = "UP" if gap_pct > 0 else "DOWN"
            if (gap_dir == "UP"   and orb_breakout == "up") \
            or (gap_dir == "DOWN" and orb_breakout == "down"):
                return gap_dir
            if gap_dir == "UP"   and delta_bias >  self.delta_bias_threshold: return "UP"
            if gap_dir == "DOWN" and delta_bias < -self.delta_bias_threshold: return "DOWN"
            return "NEUTRAL"
        if abs(gap_pct) <= self.neutral_band_pct:
            if abs(delta_bias) > self.delta_bias_threshold:
                return "UP" if delta_bias > 0 else "DOWN"
            return "NEUTRAL"
        if orb_breakout == "up"   and delta_bias >  self.delta_bias_threshold: return "UP"
        if orb_breakout == "down" and delta_bias < -self.delta_bias_threshold: return "DOWN"
        return "NEUTRAL"


# ── Feature extraction from live bars ─────────────────────────────────────────

def _compute_delta_proxy(bars: list[Bar]) -> float:
    """
    Proxy for cumulative order-flow delta from 1-min OHLCV bars.

    Convention (same as strategy-lab indicators.py):
      - If close > open → all bar volume is 'buy' (+volume)
      - If close < open → all bar volume is 'sell' (-volume)
      - If close == open → neutral (0)
    """
    delta = 0.0
    for b in bars:
        if b.close > b.open:
            delta += b.volume
        elif b.close < b.open:
            delta -= b.volume
    return delta


def _compute_vwap_slope(bars: list[Bar]) -> float:
    """
    Linear slope of the running VWAP over the given bars.
    VWAP = cumsum(typical_price × volume) / cumsum(volume).
    Slope = (vwap_last - vwap_first) / n_bars.
    Returns 0.0 if insufficient data.
    """
    if len(bars) < 2:
        return 0.0
    cum_tp_vol = 0.0
    cum_vol = 0.0
    vwaps = []
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        cum_tp_vol += tp * b.volume
        cum_vol    += b.volume
        vwaps.append(cum_tp_vol / max(cum_vol, 1))
    return (vwaps[-1] - vwaps[0]) / len(vwaps)


def extract_features(
    rth_bars: list[Bar],
    prior_ctx: PriorDayContext,
) -> Optional[dict]:
    """
    Compute Oracle feature dict from live RTH bars.

    Returns None if bars are insufficient to compute any required feature.
    """
    if not rth_bars:
        return None

    rth_open = rth_bars[0].open
    prior_close = prior_ctx.prior_close
    if prior_close <= 0:
        return None

    # gap_pct: (today_open - prior_close) / prior_close
    gap_pct = (rth_open - prior_close) / prior_close

    # ORB window
    orb_bars = rth_bars[:ORB_WINDOW_MINUTES]
    if len(orb_bars) < ORB_WINDOW_MINUTES // 2:
        return None  # not enough bars for ORB

    orb_high = max(b.high for b in orb_bars)
    orb_low  = min(b.low  for b in orb_bars)

    # Post-ORB close (bar ~15 min after ORB ends)
    post_orb_idx = ORB_WINDOW_MINUTES + POST_ORB_DELAY_MINUTES
    if post_orb_idx < len(rth_bars):
        post_orb_close = rth_bars[post_orb_idx].close
    elif rth_bars:
        post_orb_close = rth_bars[-1].close  # use latest as fallback
    else:
        post_orb_close = float("nan")

    # Delta proxy for first 30 min
    first30_bars = rth_bars[:30]
    delta_bias = _compute_delta_proxy(first30_bars)

    # VWAP slope for first 30 min
    vwap_slope = _compute_vwap_slope(first30_bars)

    # Prior-day features
    prev_day_return_pct = 0.0
    prev_close_vs_vwap  = 0.0
    if prior_ctx.prior_prior_close > 0 and prior_ctx.prior_close > 0:
        prev_day_return_pct = (prior_ctx.prior_close - prior_ctx.prior_prior_close) / prior_ctx.prior_prior_close
    if prior_ctx.prior_vwap > 0 and prior_ctx.prior_close > 0:
        prev_close_vs_vwap = (prior_ctx.prior_close - prior_ctx.prior_vwap) / prior_ctx.prior_vwap

    return {
        "gap_pct":             gap_pct,
        "orb_high":            orb_high,
        "orb_low":             orb_low,
        "post_orb_close":      post_orb_close,
        "delta_bias_first30":  delta_bias,
        "vwap_slope_first30":  vwap_slope,
        "prev_day_return_pct": prev_day_return_pct,
        "prev_close_vs_vwap":  prev_close_vs_vwap,
        "vix_close":           prior_ctx.current_vix,
    }


# ── Main classifier class ─────────────────────────────────────────────────────

class LiveClassifier:
    """
    Stateful Oracle classifier for the execution agent.

    Usage:
        clf = LiveClassifier(oracle_params=strategy.oracle.to_dict(), prior=prior_ctx)
        result = clf.maybe_classify(feed.rth_bars_today())
        if result is not None:
            # direction signal ready — proceed to spread selection
    """

    def __init__(
        self,
        oracle_params: dict,
        prior: PriorDayContext,
        min_rth_bars: int = MIN_RTH_MINUTES_BEFORE_CLASSIFY,
    ) -> None:
        self._oracle = _OracleClassifier(oracle_params)
        self._prior  = prior
        self._min_rth_bars = min_rth_bars
        self._result: Optional[ClassificationResult] = None

    @property
    def result(self) -> Optional[ClassificationResult]:
        """The current classification (None until fired)."""
        return self._result

    @property
    def has_fired(self) -> bool:
        return self._result is not None

    def maybe_classify(self, rth_bars: list[Bar]) -> Optional[ClassificationResult]:
        """
        Call on each new bar. Returns a ClassificationResult once enough RTH
        bars have accumulated, then caches the result (won't re-fire on same day).

        Returns None while waiting for more bars.
        """
        if self._result is not None:
            return self._result  # already classified today

        n = len(rth_bars)
        if n < self._min_rth_bars:
            LOGGER.debug(
                "Waiting for RTH bars: %d / %d", n, self._min_rth_bars
            )
            return None

        features = extract_features(rth_bars, self._prior)
        if features is None:
            LOGGER.warning("Feature extraction returned None with %d bars", n)
            return None

        result = self._oracle.classify(
            vix=features["vix_close"],
            gap_pct=features["gap_pct"],
            orb_breakout="none",            # recomputed internally from post_orb_close
            post_orb_close=features["post_orb_close"],
            orb_high=features["orb_high"],
            orb_low=features["orb_low"],
            delta_bias=features["delta_bias_first30"],
            vwap_slope=features["vwap_slope_first30"],
            prev_day_return_pct=features["prev_day_return_pct"],
            prev_close_vs_vwap=features["prev_close_vs_vwap"],
        )

        result.classified_at = datetime.now(EASTERN)
        result.n_rth_bars    = n
        self._result = result

        LOGGER.info(
            "Oracle classified: %s (confidence=%.2f, gap=%.4f, orb=%s, delta=%.0f, vix=%.1f)",
            result.direction, result.confidence,
            features["gap_pct"], result.orb_breakout,
            features["delta_bias_first30"], features["vix_close"],
        )
        return result

    def reset(self) -> None:
        """Call at start of each trading day to allow reclassification."""
        self._result = None
        LOGGER.debug("LiveClassifier reset for new day")

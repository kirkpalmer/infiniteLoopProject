"""
tests/test_classifier_orb.py — Verify the lookahead fix and the newly-wired
orb_breakout_pct / vwap_slope_threshold parameters.

Run from strategy-lab/:
    python -m pytest tests/test_classifier_orb.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle.classifier import OracleStrategy, OracleParams


def _row(**overrides) -> pd.Series:
    base = {
        "gap_pct": 0.0,
        "orb_breakout": "none",
        "post_orb_close": 5000.0,
        "orb_high": 5010.0,
        "orb_low": 4990.0,
        "orb_range_pct": 0.004,
        "delta_bias_first30": 0.0,
        "vix_close": 18.0,
        "prev_day_return_pct": 0.0,
        "prev_close_vs_vwap": 0.0,
        "vwap_slope_first30": 0.0,
    }
    base.update(overrides)
    return pd.Series(base)


def test_orb_breakout_pct_is_now_live():
    """The margin parameter must change classification behavior."""
    # post_orb_close 0.05% above ORB high
    row = _row(post_orb_close=5010.0 * 1.0005)

    loose = OracleStrategy(OracleParams(orb_breakout_pct=0.0001))   # margin below the move
    tight = OracleStrategy(OracleParams(orb_breakout_pct=0.0020))   # margin above the move

    assert loose._orb_direction(row) == "up"
    assert tight._orb_direction(row) == "none"   # same data, different param -> param is live


def test_orb_uses_post_orb_close_not_daily_close():
    """A day that closes UP but had no early breakout must NOT show a breakout."""
    # At minute 45 the price is inside the ORB (no breakout signal available then),
    # regardless of where the day eventually closed.
    row = _row(post_orb_close=5005.0)        # inside [4990, 5010]
    strategy = OracleStrategy(OracleParams(orb_breakout_pct=0.0))
    assert strategy._orb_direction(row) == "none"

    # Downside breakout at minute 45 is detected from post_orb_close alone
    row_dn = _row(post_orb_close=4985.0)
    assert strategy._orb_direction(row_dn) == "down"


def test_orb_fallback_for_old_feature_frames():
    """Frames built before post_orb_close existed fall back to the string column."""
    row = _row(orb_breakout="up")
    row = row.drop("post_orb_close")
    strategy = OracleStrategy()
    assert strategy._orb_direction(row) == "up"


def test_min_score_separation_kills_coin_flips():
    """A directional call with tied up/down scores must become NEUTRAL, not a coin flip."""
    # Strong gap UP + ORB up forces a directional call, but craft scores tied
    # via opposing delta: gap pushes up_score, delta pushes down_score.
    row = _row(
        gap_pct=0.004,                      # above gap threshold -> wants UP
        post_orb_close=5010.0 * 1.002,      # ORB breakout up confirms
        delta_bias_first30=-300.0,          # strong opposing flow -> down_score
    )
    no_gate = OracleStrategy(OracleParams(min_score_separation=0.0))
    gated   = OracleStrategy(OracleParams(min_score_separation=0.5))   # extreme: everything ties

    assert no_gate.classify(row).direction == "UP"
    assert gated.classify(row).direction == "NEUTRAL"   # reclassified, not skipped


def test_min_confidence_skips_weak_directional_calls():
    row = _row(
        gap_pct=0.004,
        post_orb_close=5010.0 * 1.002,
        delta_bias_first30=-300.0,          # conflicted -> low confidence
    )
    open_gate = OracleStrategy(OracleParams(min_confidence=0.0))
    strict    = OracleStrategy(OracleParams(min_confidence=0.99))

    assert open_gate.classify(row).direction == "UP"
    sig = strict.classify(row)
    assert sig.direction == "SKIP"
    assert sig.skip_reason == "low_confidence"


def test_min_confidence_does_not_skip_neutral_calls():
    """NEUTRAL calls are exempt from the confidence floor (gate 1 stays measurable)."""
    row = _row()   # everything flat -> NEUTRAL
    strict = OracleStrategy(OracleParams(min_confidence=0.99))
    assert strict.classify(row).direction == "NEUTRAL"


def test_vwap_slope_threshold_is_now_live():
    """With all primary signals flat, the VWAP slope tiebreak decides direction."""
    flat = dict(gap_pct=0.0, delta_bias_first30=0.0, post_orb_close=5000.0)

    sensitive = OracleStrategy(OracleParams(vwap_slope_threshold=0.00001))
    insensitive = OracleStrategy(OracleParams(vwap_slope_threshold=0.01))

    row_up = _row(**flat, vwap_slope_first30=0.0005)
    assert sensitive.classify(row_up).direction == "UP"
    assert insensitive.classify(row_up).direction == "NEUTRAL"

    row_dn = _row(**flat, vwap_slope_first30=-0.0005)
    assert sensitive.classify(row_dn).direction == "DOWN"

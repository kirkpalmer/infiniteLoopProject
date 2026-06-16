"""
tests/test_walkforward.py — Walk-forward validation, skip reasons, and
confidence buckets.

Run from strategy-lab/:
    python -m pytest tests/test_walkforward.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle.classifier import OracleStrategy, OracleParams
from oracle.backtest import run_oracle_backtest, confidence_buckets
from oracle.walkforward import run_walk_forward


def _features(n: int = 240, vix_low: float = 14, vix_high: float = 25) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "gap_pct":            rng.normal(0, 0.004, n),
        "orb_breakout":       rng.choice(["up", "down", "none"], n),
        "post_orb_close":     5000 + rng.normal(0, 15, n),
        "delta_bias_first30": rng.normal(0, 250, n),
        "orb_high":           5000 + np.abs(rng.normal(8, 3, n)),
        "orb_low":            5000 - np.abs(rng.normal(8, 3, n)),
        "orb_range_pct":      np.abs(rng.normal(0.002, 0.001, n)),
        "vix_close":          rng.uniform(vix_low, vix_high, n),
        "vwap_slope_first30": rng.normal(0, 0.0003, n),
        "outcome":            rng.choice(["UP", "DOWN", "NEUTRAL"], n),
    }, index=idx)


def test_skip_reasons_are_reported():
    feats = _features(100)
    # Push half the days above the VIX ceiling and break features on a few
    feats.iloc[:50, feats.columns.get_loc("vix_close")] = 50.0
    feats.iloc[95:, feats.columns.get_loc("gap_pct")] = np.nan

    results = run_oracle_backtest(feats, OracleStrategy(), "outcome")
    assert results.skip_reasons.get("vix_above_high", 0) == 50
    assert results.skip_reasons.get("missing_features", 0) == 5
    assert results.skip_rate == pytest.approx(0.55)


def test_confidence_buckets_shape():
    feats = _features(200)
    results = run_oracle_backtest(feats, OracleStrategy(), "outcome")
    buckets = confidence_buckets(results.raw)
    assert 1 <= len(buckets) <= 4
    for b in buckets:
        assert set(b) == {"confidence_range", "days", "accuracy"}
        assert b["days"] > 0
        assert 0.0 <= b["accuracy"] <= 1.0
    # All active days accounted for
    assert sum(b["days"] for b in buckets) == results.trade_days


def test_walk_forward_folds():
    feats = _features(240)
    report = run_walk_forward(feats, OracleStrategy(), n_folds=6)
    assert len(report.folds) == 6
    # Chronological, non-overlapping coverage of all rows
    assert sum(f["total_days"] for f in report.folds) == 240
    assert report.folds[0]["start"] < report.folds[-1]["end"]
    assert report.verdict != "insufficient_data"
    assert report.to_dict()["folds"] == report.folds


def test_walk_forward_flags_thin_folds():
    """Folds destroyed by the VIX gate must be marked thin, not averaged in."""
    feats = _features(240)
    # Last third of history: VIX 50 -> everything skipped there
    feats.iloc[160:, feats.columns.get_loc("vix_close")] = 50.0
    report = run_walk_forward(feats, OracleStrategy(), n_folds=6)
    assert report.thin_folds >= 2
    thin = [f for f in report.folds if f["thin"]]
    assert all(f["skip_reasons"].get("vix_above_high", 0) > 0 for f in thin)


def test_walk_forward_insufficient_data():
    report = run_walk_forward(_features(40), OracleStrategy(), n_folds=6)
    assert report.verdict == "insufficient_data"

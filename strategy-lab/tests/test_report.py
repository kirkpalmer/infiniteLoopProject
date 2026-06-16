"""tests/test_report.py — Smoke test for the markdown status report."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle.classifier import OracleStrategy
from oracle.backtest import run_oracle_backtest, confidence_buckets
from oracle.walkforward import run_walk_forward
from oracle.report import build_report


def test_report_builds_with_full_inputs():
    rng = np.random.default_rng(3)
    n = 150
    idx = pd.date_range("2024-06-01", periods=n, freq="B")
    feats = pd.DataFrame({
        "gap_pct":            rng.normal(0, 0.004, n),
        "orb_breakout":       rng.choice(["up", "down", "none"], n),
        "post_orb_close":     5000 + rng.normal(0, 15, n),
        "delta_bias_first30": rng.normal(0, 250, n),
        "orb_high":           5000 + np.abs(rng.normal(8, 3, n)),
        "orb_low":            5000 - np.abs(rng.normal(8, 3, n)),
        "orb_range_pct":      np.abs(rng.normal(0.002, 0.001, n)),
        "vix_close":          rng.uniform(14, 25, n),
        "vwap_slope_first30": rng.normal(0, 0.0003, n),
        "outcome":            rng.choice(["UP", "DOWN", "NEUTRAL"], n),
    }, index=idx)

    strategy = OracleStrategy()
    is_results = run_oracle_backtest(feats.iloc[:120], strategy, "outcome")
    oos_results = run_oracle_backtest(feats.iloc[120:], strategy, "outcome")
    wf = run_walk_forward(feats, strategy, n_folds=3).to_dict()

    days = [{
        "date": "2026-01-02", "call": "UP", "actual": "UP", "correct": True,
        "skipped": False, "skip_reason": "", "confidence": 0.62,
        "up_score": 0.6, "down_score": 0.2, "neutral_score": 0.2, "lean": "NONE",
    }]
    days_payload = {
        "days": days, "skip_rate": 0.1, "skip_reasons": {"low_confidence": 3},
        "confidence_buckets": confidence_buckets(oos_results.raw),
    }
    eligibility = {
        "passed": False, "score": "7/9",
        "criteria": [{
            "name": "min_trades", "description": "sample size", "value": 97,
            "threshold": 200, "passed": False, "direction": "above", "gap": -103,
        }],
    }
    runs = [{
        "run_id": 1, "started_at": "2026-06-11", "finished_at": "2026-06-11",
        "best_accuracy": 0.68, "total_iterations": 350, "accepted_count": 11,
        "notes": "optuna sweep",
    }]
    sweep = {"when": "2026-06-11 12:00", "n_trials": 350, "failed_trials": 80,
             "importances": {"min_confidence": 0.41, "gap_threshold_pct": 0.2}}

    text = build_report(
        params=strategy.get_params(), is_results=is_results, oos_results=oos_results,
        eligibility=eligibility, walkforward=wf, days_payload=days_payload,
        run_summary=runs, last_sweep=sweep, registry_backend="postgres",
        is_rows=120, oos_rows=30,
    )

    for marker in (
        "# InfiniteLoop — Oracle Status Report",
        "## Current Parameters",
        "## In-Sample Performance",
        "## Out-of-Sample Performance",
        "IS/OOS drift",
        "## Eligibility Gate",
        "## Walk-Forward Validation",
        "## Daily Signals — OOS",
        "## Last Optuna Sweep",
        "min_confidence: 41.0%",
        "## Recent Optimization Runs",
    ):
        assert marker in text, marker


def test_report_builds_with_minimal_inputs():
    rng = np.random.default_rng(4)
    n = 60
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    feats = pd.DataFrame({
        "gap_pct": rng.normal(0, 0.004, n), "orb_breakout": "none",
        "post_orb_close": 5000.0, "delta_bias_first30": 0.0,
        "orb_high": 5010.0, "orb_low": 4990.0, "orb_range_pct": 0.004,
        "vix_close": 18.0, "vwap_slope_first30": 0.0,
        "outcome": rng.choice(["UP", "DOWN", "NEUTRAL"], n),
    }, index=idx)
    is_results = run_oracle_backtest(feats, OracleStrategy(), "outcome")

    text = build_report(
        params={}, is_results=is_results, oos_results=None, eligibility=None,
        walkforward=None, days_payload=None, run_summary=None, last_sweep=None,
        registry_backend="sqlite", is_rows=n, oos_rows=0,
    )
    assert "## In-Sample Performance" in text
    assert "Out-of-Sample" not in text

"""
tests/test_optimize.py — Verify the Optuna sweep: trials run, guardrails hold,
every trial persists to the registry, seeding works, and stop is honored.

Run from strategy-lab/:
    python -m pytest tests/test_optimize.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.oracle_registry import SqliteOracleRegistry
from oracle.optimize import run_oracle_sweep, macro_accuracy, _seed_trial_from_params, SEARCH_SPACE
from oracle.classifier import OracleStrategy


@pytest.fixture()
def registry(tmp_path):
    reg = SqliteOracleRegistry(tmp_path / "oracle_history.db")
    reg.ensure_schema()
    return reg


@pytest.fixture()
def features():
    rng = np.random.default_rng(7)
    n = 80
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "gap_pct":            rng.normal(0, 0.004, n),
        "orb_breakout":       rng.choice(["up", "down", "none"], n),
        "delta_bias_first30": rng.normal(0, 250, n),
        "orb_high":           5000 + rng.normal(0, 10, n),
        "orb_low":            4990 + rng.normal(0, 10, n),
        "orb_range_pct":      np.abs(rng.normal(0.002, 0.001, n)),
        "vix_close":          rng.uniform(14, 25, n),
        "outcome":            rng.choice(["UP", "DOWN", "NEUTRAL"], n),
    }, index=idx)


def test_sweep_runs_and_persists(registry, features):
    result = run_oracle_sweep(
        features, n_trials=12, oracle_registry=registry, run_notes="test sweep",
    )
    assert result.n_trials == 12
    counts = registry.counts()
    assert counts == {"runs": 1, "iterations": 12}    # EVERY trial persisted

    # Run record closed with the best result
    summary = registry.get_run_summary()[0]
    assert summary["finished_at"] is not None
    assert summary["total_iterations"] == 12
    assert summary["notes"] == "test sweep"

    # Best params are a complete, valid Oracle param set
    strategy = OracleStrategy.from_params(result.best_params)
    assert strategy.get_params()["gap_threshold_pct"] > 0
    # Constraint held by construction
    assert result.best_params["neutral_band_pct"] < result.best_params["gap_threshold_pct"]


def test_sweep_seeds_from_registry_best(registry, features):
    """A second sweep must seed from the best params found previously."""
    run_oracle_sweep(features, n_trials=6, oracle_registry=registry)
    best_before = registry.get_best_params_ever()
    assert best_before is not None

    captured = {}

    def on_trial(num, record, best):
        if num == 1:
            captured["first_trial_params"] = record

    run_oracle_sweep(features, n_trials=3, oracle_registry=registry, on_trial=on_trial)
    # Trial 1 of run 2 is the enqueued seed — its full_params in the DB should
    # match the prior best (within float/clamping tolerance on gap threshold).
    history = registry.load_cross_session_history(limit=3)
    first_of_run2 = [h for h in history if h["run_id"] == 2 and h["iteration"] == 1][0]
    assert abs(
        first_of_run2["full_params"]["gap_threshold_pct"]
        - best_before["gap_threshold_pct"]
    ) < 1e-9


def test_sweep_stop_via_callback(registry, features):
    """on_trial raising StopIteration stops the sweep early but still finishes the run."""
    def stop_after_3(num, record, best):
        if num >= 3:
            raise StopIteration

    result = run_oracle_sweep(
        features, n_trials=50, oracle_registry=registry, on_trial=stop_after_3,
    )
    assert result.n_trials < 50
    assert registry.get_run_summary()[0]["finished_at"] is not None


def test_seed_inversion_roundtrip():
    params = OracleStrategy().get_params()
    seed = _seed_trial_from_params(params)
    assert seed is not None
    for name, value in seed.items():
        space = SEARCH_SPACE[name]
        assert space["low"] <= value <= space["high"]
    # frac inversion: neutral_band = gap * frac
    assert abs(seed["gap_threshold_pct"] * seed["neutral_band_frac"]
               - params["neutral_band_pct"]) < 1e-9


def test_macro_accuracy_balances_classes():
    from oracle.backtest import OracleResults
    lopsided = OracleResults(
        overall_accuracy=0.70, up_accuracy=0.95, down_accuracy=0.05,
        neutral_accuracy=0.80, skip_rate=0.0, trade_days=300, total_days=300,
        up_count=200, down_count=50, neutral_count=50,
    )
    # Raw accuracy looks fine; macro exposes the dead DOWN class
    assert lopsided.overall_accuracy == 0.70
    assert macro_accuracy(lopsided) == pytest.approx(0.60)


def test_precision_is_computed_from_confusion():
    """Precision (trade-signal hit rate) vs recall — the June 12 lesson:
    a NEUTRAL-spamming config can have terrible recall but great precision."""
    from oracle.backtest import OracleResults
    confusion = {
        # actual -> predicted counts (NEUTRAL spam: directional recall poor)
        "UP":      {"UP": 60, "DOWN": 5, "NEUTRAL": 100},
        "DOWN":    {"UP": 10, "DOWN": 40, "NEUTRAL": 90},
        "NEUTRAL": {"UP": 2,  "DOWN": 3, "NEUTRAL": 20},
    }
    r = OracleResults(
        overall_accuracy=0.36, up_accuracy=60/165, down_accuracy=40/140,
        neutral_accuracy=0.8, skip_rate=0.0, trade_days=330, total_days=330,
        up_count=165, down_count=140, neutral_count=25, confusion=confusion,
    )
    assert r.up_calls == 72
    assert r.down_calls == 48
    assert r.directional_calls == 120
    assert r.up_precision == pytest.approx(60 / 72)
    assert r.down_precision == pytest.approx(40 / 48)
    assert r.directional_precision == pytest.approx(100 / 120)   # 83% hit rate
    # ...even though directional RECALL is poor (UP 36%, DOWN 29%)
    assert r.up_accuracy < 0.40 and r.down_accuracy < 0.30

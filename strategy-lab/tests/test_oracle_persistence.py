"""
tests/test_oracle_persistence.py — Verify the Oracle cross-session memory chain:

  Hermes loop → registry persistence → reload as tried-values / history → next run
  skips duplicates and seeds from best params.

Run from strategy-lab/:
    python -m pytest tests/test_oracle_persistence.py -v

Uses the SQLite registry backend (same interface as Postgres) and a scripted
fake Hermes — no Ollama, no network, no real market data needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401  — loads .env (harmless in tests)
from strategy.oracle_registry import SqliteOracleRegistry, open_oracle_registry
from oracle.records import IterationRecord, record_to_ws, db_iteration_to_ws
import oracle.hermes_loop as hl
from oracle.classifier import OracleStrategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry(tmp_path):
    reg = SqliteOracleRegistry(tmp_path / "oracle_history.db")
    reg.ensure_schema()
    return reg


@pytest.fixture()
def features():
    """Synthetic labeled feature frame the classifier can score."""
    rng = np.random.default_rng(42)
    n = 60
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    gap = rng.normal(0, 0.004, n)
    frame = pd.DataFrame({
        "gap_pct":            gap,
        "orb_breakout":       rng.choice(["up", "down", "none"], n),
        "delta_bias_first30": rng.normal(0, 250, n),
        "orb_high":           5000 + rng.normal(0, 10, n),
        "orb_low":            4990 + rng.normal(0, 10, n),
        "orb_range_pct":      np.abs(rng.normal(0.002, 0.001, n)),
        "vix_close":          rng.uniform(14, 25, n),
        "outcome":            rng.choice(["UP", "DOWN", "NEUTRAL"], n),
    }, index=idx)
    return frame


class FakeHermes:
    """Scripted Hermes: returns queued responses, then repeats the last one."""
    script: list[dict] = []
    calls: int = 0

    def __init__(self, *a, **k): ...
    def is_available(self) -> bool: return True

    def generate_json(self, prompt: str) -> dict:
        FakeHermes.calls += 1
        i = min(FakeHermes.calls - 1, len(FakeHermes.script) - 1)
        return FakeHermes.script[i]


@pytest.fixture()
def fake_hermes(monkeypatch):
    FakeHermes.calls = 0
    monkeypatch.setattr(hl, "HermesClient", FakeHermes)
    return FakeHermes


def _change(param, value):
    return {"reasoning": "test", "param_to_change": param, "new_value": value,
            "change_summary": f"{param} -> {value}"}


# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------

def test_registry_roundtrip(registry):
    run_id = registry.create_run({"gap_threshold_pct": 0.0025}, notes="unit test")
    assert run_id == 1

    registry.save_iteration(
        run_id=run_id, iteration=1, param_changed="gap_threshold_pct",
        old_value=0.0025, new_value=0.003,
        overall_accuracy=0.61, up_accuracy=0.65, down_accuracy=0.55,
        neutral_accuracy=0.60, skip_rate=0.05, accepted=True,
        hermes_reasoning="better", full_params={"gap_threshold_pct": 0.003},
    )
    registry.save_iteration(
        run_id=run_id, iteration=2, param_changed="vol_filter_high",
        old_value=32.0, new_value=28.0,
        overall_accuracy=0.55, up_accuracy=0.5, down_accuracy=0.5,
        neutral_accuracy=0.6, skip_rate=0.1, accepted=False,
        hermes_reasoning="worse", full_params={"gap_threshold_pct": 0.003, "vol_filter_high": 28.0},
    )
    registry.finish_run(run_id, {"gap_threshold_pct": 0.003}, 0.61, 2)

    history = registry.load_cross_session_history(limit=10)
    assert len(history) == 2
    assert history[0]["iteration"] == 1                      # chronological
    assert history[0]["full_params"] == {"gap_threshold_pct": 0.003}
    assert history[0]["accepted"] is True
    assert history[1]["accepted"] is False

    best = registry.get_best_params_ever()
    assert best == {"gap_threshold_pct": 0.003}

    tried = registry.get_tried_values()
    assert "gap_threshold_pct" in tried and "vol_filter_high" in tried

    rejected = registry.get_rejected_values()
    assert rejected == {"vol_filter_high": [28.0]}

    counts = registry.counts()
    assert counts == {"runs": 1, "iterations": 2}

    summary = registry.get_run_summary()
    assert summary[0]["accepted_count"] == 1
    assert summary[0]["finished_at"] is not None


def test_open_oracle_registry_falls_back_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    backend, reg = open_oracle_registry(sqlite_path=tmp_path / "fallback.db")
    assert backend == "sqlite"
    assert reg.ping()


# ---------------------------------------------------------------------------
# Wire format — one canonical shape
# ---------------------------------------------------------------------------

def test_ws_shapes_are_identical(registry):
    rec = IterationRecord(
        iteration=3, param_changed="gap_threshold_pct", old_value=0.0025,
        new_value=0.003, accuracy=0.6, up_accuracy=0.6, down_accuracy=0.6,
        neutral_accuracy=0.6, skip_rate=0.0, accepted=True,
        hermes_reasoning="r", run_id=1,
    )
    live = record_to_ws(rec, seq=7)

    run_id = registry.create_run({}, "")
    registry.save_iteration(
        run_id=run_id, iteration=3, param_changed="gap_threshold_pct",
        old_value=0.0025, new_value=0.003, overall_accuracy=0.6,
        up_accuracy=0.6, down_accuracy=0.6, neutral_accuracy=0.6,
        skip_rate=0.0, accepted=True, hermes_reasoning="r", full_params={},
    )
    row = registry.load_cross_session_history(limit=1)[0]
    hydrated = db_iteration_to_ws(row, seq=7)

    assert set(live.keys()) == set(hydrated.keys())
    for key in ("n", "run_iter", "p", "o", "v", "acc", "ok", "r"):
        assert live[key] == hydrated[key], key


# ---------------------------------------------------------------------------
# Loop persistence + cross-session duplicate skipping
# ---------------------------------------------------------------------------

def test_loop_persists_iterations(registry, features, fake_hermes):
    fake_hermes.script = [
        _change("gap_threshold_pct", 0.003),       # evaluated
        _change("vol_filter_high", 28.0),          # evaluated
        _change("entry_hour", 11),                 # forbidden -> parse-failure path
        _change("delta_bias_threshold", 200.0),    # evaluated (after corrective retry)
        _change("delta_bias_threshold", 200.0),    # duplicate -> skipped, no backtest
        _change("neutral_band_pct", 0.001),        # evaluated
    ]
    best, history = hl.run_oracle_loop(
        features, max_iterations=6, oracle_registry=registry, run_notes="test run 1",
    )
    # 4 evaluated iterations persisted (forbidden + duplicate produce no rows)
    assert registry.counts() == {"runs": 1, "iterations": 4}
    assert len(history) == 4
    assert all(r.run_id == 1 for r in history)

    runs = registry.get_run_summary()
    assert runs[0]["finished_at"] is not None     # finish_run always called
    assert runs[0]["total_iterations"] == 4


def test_second_run_remembers_first(registry, features, fake_hermes):
    # Run 1 tests gap 0.003
    fake_hermes.script = [_change("gap_threshold_pct", 0.003)]
    hl.run_oracle_loop(features, max_iterations=1, oracle_registry=registry)
    assert registry.counts()["iterations"] == 1

    # Run 2 proposes the SAME value -> must be skipped via DB-loaded memory,
    # then proposes a new one -> evaluated.
    fake_hermes.calls = 0
    fake_hermes.script = [
        _change("gap_threshold_pct", 0.003),   # known from run 1 -> duplicate skip
        _change("gap_threshold_pct", 0.005),   # new -> evaluated
    ]
    hl.run_oracle_loop(features, max_iterations=2, oracle_registry=registry)

    counts = registry.counts()
    assert counts["runs"] == 2
    assert counts["iterations"] == 2   # only ONE new row from run 2

    # Both runs closed properly
    assert all(r["finished_at"] is not None for r in registry.get_run_summary())


def test_hermes_prompt_contains_db_history(registry, features, fake_hermes):
    """The core question: does Hermes actually SEE past iterations?

    Seed the DB with a fake prior session, then capture the prompt text the
    loop sends to Hermes and assert the persisted values appear in it.
    """
    # Simulate a prior session: one accepted, one rejected iteration
    run_id = registry.create_run({"gap_threshold_pct": 0.0025}, notes="prior session")
    registry.save_iteration(
        run_id=run_id, iteration=1, param_changed="gap_threshold_pct",
        old_value=0.0025, new_value=0.00417,
        overall_accuracy=0.6123, up_accuracy=0.6, down_accuracy=0.6,
        neutral_accuracy=0.6, skip_rate=0.0, accepted=True,
        hermes_reasoning="prior accepted", full_params={"gap_threshold_pct": 0.00417},
    )
    registry.save_iteration(
        run_id=run_id, iteration=2, param_changed="vol_filter_high",
        old_value=32.0, new_value=27.77,
        overall_accuracy=0.41, up_accuracy=0.4, down_accuracy=0.4,
        neutral_accuracy=0.4, skip_rate=0.2, accepted=False,
        hermes_reasoning="prior rejected", full_params={"gap_threshold_pct": 0.00417, "vol_filter_high": 27.77},
    )
    registry.finish_run(run_id, {"gap_threshold_pct": 0.00417}, 0.6123, 2)

    # Capture every prompt the loop sends to Hermes
    prompts: list[str] = []
    orig = fake_hermes.generate_json

    def capture(self, prompt: str) -> dict:
        prompts.append(prompt)
        return orig(self, prompt)

    fake_hermes.generate_json = capture
    fake_hermes.script = [
        _change("neutral_band_pct", 0.0015),
        _change("neutral_band_pct", 0.0018),
    ]
    try:
        hl.run_oracle_loop(features, max_iterations=2, oracle_registry=registry)
    finally:
        fake_hermes.generate_json = orig

    assert len(prompts) >= 2
    initial, iteration2 = prompts[0], prompts[1]

    # Initial prompt: full cross-session context from the DB
    assert "PRIOR RUN HISTORY" in initial
    assert "VALUES ALREADY TESTED" in initial
    assert "BEST PARAMS EVER FOUND" in initial
    assert "0.00417" in initial          # accepted value from prior session
    assert "27.77" in initial            # rejected value from prior session
    assert "0.6123" in initial           # accuracy it produced

    # Iteration prompts keep showing the tried-values map from the DB
    assert "VALUES ALREADY TESTED" in iteration2
    assert "0.00417" in iteration2 and "27.77" in iteration2
    # ...plus what happened earlier in this session
    assert "0.0015" in iteration2

    # And the loop SEEDED from the prior session's best params
    assert "Current parameters" in initial
    assert '"gap_threshold_pct": 0.00417' in initial

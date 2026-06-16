"""
oracle/records.py — Canonical Oracle iteration record + conversions.

There is exactly ONE wire format for an iteration shown in the dashboard
(the "ws dict", short keys to keep WebSocket payloads small):

    n        global sequence number (unique across runs/sessions — chart x-axis)
    run_iter iteration number within its run (restarts at 1 every run)
    run_id   oracle_runs.id (None if not persisted)
    p        param changed
    o        old value
    v        new value
    acc      overall accuracy
    up/dn/neu  per-class accuracy
    skip     skip rate
    ok       accepted (bool)
    r        hermes reasoning

Everything that produces an iteration for the UI MUST go through
`record_to_ws()` or `db_iteration_to_ws()`. Never hand-build these dicts —
field-name drift between server and dashboard is what broke the site before.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IterationRecord:
    """In-memory result of one Hermes loop iteration."""
    iteration: int                 # run-local iteration number (1-based)
    param_changed: str
    old_value: object
    new_value: object
    accuracy: float
    up_accuracy: float
    down_accuracy: float
    neutral_accuracy: float
    skip_rate: float
    accepted: bool
    hermes_reasoning: str
    run_id: int | None = None      # set when persisted


def record_to_ws(record: IterationRecord, seq: int) -> dict:
    """Convert a live IterationRecord to the canonical dashboard dict."""
    return {
        "n":        seq,
        "run_iter": record.iteration,
        "run_id":   record.run_id,
        "p":        record.param_changed,
        "o":        record.old_value,
        "v":        record.new_value,
        "acc":      record.accuracy,
        "up":       record.up_accuracy,
        "dn":       record.down_accuracy,
        "neu":      record.neutral_accuracy,
        "skip":     record.skip_rate,
        "ok":       record.accepted,
        "r":        record.hermes_reasoning,
    }


def db_iteration_to_ws(row: dict, seq: int) -> dict:
    """Convert a registry history row (long keys) to the canonical dashboard dict."""
    return {
        "n":        seq,
        "run_iter": row.get("iteration"),
        "run_id":   row.get("run_id"),
        "p":        row.get("param_changed"),
        "o":        row.get("old_value"),
        "v":        row.get("new_value"),
        "acc":      row.get("overall_accuracy"),
        "up":       row.get("up_accuracy"),
        "dn":       row.get("down_accuracy"),
        "neu":      row.get("neutral_accuracy"),
        "skip":     row.get("skip_rate"),
        "ok":       bool(row.get("accepted")),
        "r":        row.get("hermes_reasoning") or "",
    }

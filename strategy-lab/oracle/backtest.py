"""
oracle/backtest.py — Oracle direction accuracy backtester.

Runs the Oracle classifier over labeled historical feature data and produces
a scorecard with overall and per-class directional accuracy + confusion matrix.

This module deliberately knows nothing about spread P&L, strike selection,
or position sizing. Those belong to the trade agent (Phase 2).

Key output metric: directional accuracy per class (UP, DOWN, NEUTRAL).
SKIP days are excluded from accuracy calculations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .classifier import OracleStrategy

LOGGER = logging.getLogger("infiniteloop.oracle.backtest")

# Labels we score against
DIRECTION_CLASSES = ("UP", "DOWN", "NEUTRAL")


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class OracleResults:
    """
    Scorecard from one Oracle backtest run.

    All accuracy values are fractions in [0, 1].
    Confusion matrix rows = actual, columns = predicted.
    """
    # Summary metrics
    overall_accuracy: float
    up_accuracy: float
    down_accuracy: float
    neutral_accuracy: float
    skip_rate: float            # fraction of days Oracle returned SKIP
    trade_days: int             # days Oracle made a call (not SKIP)
    total_days: int             # all days in the test window

    # Per-class sample counts (actual labels)
    up_count: int
    down_count: int
    neutral_count: int

    # Average confidence score across non-skipped days
    avg_confidence: float = 0.0

    # Confusion matrix as dict-of-dicts: confusion[actual][predicted] = count
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    # Why days were skipped: {reason: count}
    skip_reasons: dict[str, int] = field(default_factory=dict)

    # Raw predictions DataFrame (date, predicted, actual, correct)
    raw: Optional[pd.DataFrame] = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # PRECISION — the metric that matters for trading.
    # Recall asks "of all UP days, how many did we catch?" Precision asks
    # "when Oracle CALLS UP, how often is it right?" — and we only trade
    # when Oracle makes a call.
    # ------------------------------------------------------------------

    def _calls(self, cls: str) -> int:
        """How many times `cls` was PREDICTED (column sum of confusion)."""
        return sum(self.confusion.get(actual, {}).get(cls, 0) for actual in DIRECTION_CLASSES)

    def _precision(self, cls: str) -> float:
        calls = self._calls(cls)
        return self.confusion.get(cls, {}).get(cls, 0) / calls if calls else 0.0

    @property
    def up_calls(self) -> int: return self._calls("UP")

    @property
    def down_calls(self) -> int: return self._calls("DOWN")

    @property
    def directional_calls(self) -> int: return self.up_calls + self.down_calls

    @property
    def up_precision(self) -> float: return self._precision("UP")

    @property
    def down_precision(self) -> float: return self._precision("DOWN")

    @property
    def directional_precision(self) -> float:
        """Correct UP+DOWN calls / all UP+DOWN calls — the trade-signal hit rate."""
        calls = self.directional_calls
        if not calls:
            return 0.0
        correct = (self.confusion.get("UP", {}).get("UP", 0)
                   + self.confusion.get("DOWN", {}).get("DOWN", 0))
        return correct / calls

    def summary_dict(self) -> dict:
        """Return a flat dict suitable for Hermes prompt history."""
        return {
            "overall_accuracy": round(self.overall_accuracy, 4),
            "up_accuracy": round(self.up_accuracy, 4),
            "down_accuracy": round(self.down_accuracy, 4),
            "neutral_accuracy": round(self.neutral_accuracy, 4),
            "directional_precision": round(self.directional_precision, 4),
            "up_precision": round(self.up_precision, 4),
            "down_precision": round(self.down_precision, 4),
            "directional_calls": self.directional_calls,
            "skip_rate": round(self.skip_rate, 4),
            "trade_days": self.trade_days,
            "total_days": self.total_days,
            "class_distribution": {
                "UP": self.up_count,
                "DOWN": self.down_count,
                "NEUTRAL": self.neutral_count,
            },
        }

    def passed_min_threshold(self, min_accuracy: float = 0.55) -> bool:
        return self.overall_accuracy >= min_accuracy and self.trade_days >= 50


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def run_oracle_backtest(
    features: pd.DataFrame,
    strategy: OracleStrategy,
    outcome_col: str = "outcome",
) -> OracleResults:
    """
    Run Oracle classification over the feature frame and score against labeled outcomes.

    Args:
        features:    DataFrame with direction features + outcome column.
                     Produced by oracle/outcomes.py label_outcomes().
        strategy:    OracleStrategy instance to evaluate.
        outcome_col: Column name for the labeled outcome (default: 'outcome').

    Returns:
        OracleResults scorecard.
    """
    if features.empty:
        LOGGER.warning("Empty features DataFrame — returning zero scorecard")
        return _empty_results()

    if outcome_col not in features.columns:
        raise ValueError(
            f"Column '{outcome_col}' not found. Run oracle/outcomes.py first to label features."
        )

    # Run classification — classify_df returns a DataFrame with
    # columns: predicted, confidence, up_score, down_score, neutral_score, lean
    signals_df = strategy.classify_df(features)

    raw_rows = []
    for date, row in features.iterrows():
        sig = signals_df.loc[date]
        predicted  = sig["predicted"]
        actual     = row[outcome_col]
        raw_rows.append({
            "date":          date,
            "predicted":     predicted,
            "actual":        actual,
            "correct":       predicted == actual and predicted != "SKIP",
            "skipped":       predicted == "SKIP",
            "skip_reason":   sig.get("skip_reason", ""),
            "confidence":    sig.get("confidence", 0.0),
            "up_score":      sig.get("up_score", 0.0),
            "down_score":    sig.get("down_score", 0.0),
            "neutral_score": sig.get("neutral_score", 0.0),
            "lean":          sig.get("lean", "NONE"),
        })
    raw = pd.DataFrame(raw_rows)

    total_days = len(raw)
    skip_days = int(raw["skipped"].sum())
    trade_days = total_days - skip_days
    active = raw[~raw["skipped"]].copy()

    overall_accuracy = float(active["correct"].mean()) if not active.empty else 0.0

    per_class_acc = {}
    per_class_count = {}
    for label in DIRECTION_CLASSES:
        subset = active[active["actual"] == label]
        per_class_count[label] = len(subset)
        per_class_acc[label] = float(subset["correct"].mean()) if not subset.empty else 0.0

    confusion = _build_confusion(active)

    skipped_rows = raw[raw["skipped"]]
    skip_reasons = (
        skipped_rows["skip_reason"].replace("", "unknown").value_counts().to_dict()
        if not skipped_rows.empty else {}
    )

    avg_confidence = float(active["confidence"].mean()) if "confidence" in active.columns and not active.empty else 0.0

    results = OracleResults(
        overall_accuracy=overall_accuracy,
        up_accuracy=per_class_acc["UP"],
        down_accuracy=per_class_acc["DOWN"],
        neutral_accuracy=per_class_acc["NEUTRAL"],
        skip_rate=skip_days / max(total_days, 1),
        trade_days=trade_days,
        total_days=total_days,
        up_count=per_class_count["UP"],
        down_count=per_class_count["DOWN"],
        neutral_count=per_class_count["NEUTRAL"],
        avg_confidence=avg_confidence,
        confusion=confusion,
        skip_reasons=skip_reasons,
        raw=raw,
    )

    LOGGER.info(
        "Oracle backtest: %d days | accuracy=%.2f%% | UP=%.2f%% DOWN=%.2f%% NEUTRAL=%.2f%% | skip=%.2f%%",
        trade_days,
        overall_accuracy * 100,
        per_class_acc["UP"] * 100,
        per_class_acc["DOWN"] * 100,
        per_class_acc["NEUTRAL"] * 100,
        results.skip_rate * 100,
    )
    return results


def run_oracle_oos_backtest(
    features: pd.DataFrame,
    strategy: OracleStrategy,
    oos_fraction: float = 0.20,
    outcome_col: str = "outcome",
) -> tuple[OracleResults, OracleResults]:
    """
    Split features into in-sample (IS) and out-of-sample (OOS) sets and backtest both.

    The last oos_fraction of data is OOS. NEVER use OOS data during Hermes optimization —
    only call this for final validation after Hermes has converged.

    Returns:
        (is_results, oos_results)
    """
    if features.empty:
        empty = _empty_results()
        return empty, empty

    split_idx = int(len(features) * (1.0 - oos_fraction))
    is_features = features.iloc[:split_idx].copy()
    oos_features = features.iloc[split_idx:].copy()

    LOGGER.info(
        "OOS split: IS=%d days, OOS=%d days (%.0f%% / %.0f%%)",
        len(is_features), len(oos_features),
        (1 - oos_fraction) * 100, oos_fraction * 100,
    )

    is_results = run_oracle_backtest(is_features, strategy, outcome_col)
    oos_results = run_oracle_backtest(oos_features, strategy, outcome_col)

    drift = abs(is_results.overall_accuracy - oos_results.overall_accuracy)
    LOGGER.info(
        "IS accuracy=%.2f%% | OOS accuracy=%.2f%% | drift=%.2f%%",
        is_results.overall_accuracy * 100,
        oos_results.overall_accuracy * 100,
        drift * 100,
    )
    if drift > 0.10:
        LOGGER.warning(
            "IS/OOS drift %.2f%% exceeds 10%% threshold — possible overfitting", drift * 100
        )

    return is_results, oos_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_confusion(active: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Build confusion matrix as nested dict: confusion[actual][predicted] = count."""
    matrix: dict[str, dict[str, int]] = {
        cls: {p: 0 for p in DIRECTION_CLASSES} for cls in DIRECTION_CLASSES
    }
    for _, row in active.iterrows():
        actual = row["actual"]
        predicted = row["predicted"]
        if actual in matrix and predicted in matrix:
            matrix[actual][predicted] += 1
    return matrix


def confidence_buckets(raw: pd.DataFrame, n_buckets: int = 4) -> list[dict]:
    """
    Accuracy by confidence quartile over non-skipped days.

    This is the key input for the (Phase 2) trade agent: if accuracy rises
    monotonically with confidence, confidence-gated position taking works —
    trade only above a confidence floor, size up with conviction.
    """
    if raw is None or raw.empty:
        return []
    active = raw[~raw["skipped"]].copy()
    if len(active) < n_buckets * 2:
        return []
    try:
        active["bucket"] = pd.qcut(active["confidence"], n_buckets, duplicates="drop")
    except ValueError:
        return []
    out = []
    for interval, group in active.groupby("bucket", observed=True):
        out.append({
            "confidence_range": f"{float(interval.left):.2f}–{float(interval.right):.2f}",
            "days": int(len(group)),
            "accuracy": round(float(group["correct"].mean()), 4),
        })
    return out


def confusion_to_dataframe(confusion: dict[str, dict[str, int]]) -> pd.DataFrame:
    """Convert confusion dict to a labeled DataFrame for display."""
    return pd.DataFrame(confusion).T.reindex(
        index=DIRECTION_CLASSES, columns=DIRECTION_CLASSES, fill_value=0
    )


def _empty_results() -> OracleResults:
    empty_confusion = {cls: {p: 0 for p in DIRECTION_CLASSES} for cls in DIRECTION_CLASSES}
    return OracleResults(
        overall_accuracy=0.0,
        up_accuracy=0.0,
        down_accuracy=0.0,
        neutral_accuracy=0.0,
        skip_rate=1.0,
        trade_days=0,
        total_days=0,
        up_count=0,
        down_count=0,
        neutral_count=0,
        avg_confidence=0.0,
        confusion=empty_confusion,
        raw=pd.DataFrame(),
    )

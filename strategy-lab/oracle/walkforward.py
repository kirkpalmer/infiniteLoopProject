"""
oracle/walkforward.py — Walk-forward validation for the Oracle classifier.

A single 80/20 OOS split is one noisy sample (39 active OOS days produced a
±16-point confidence interval). Walk-forward slices the FULL history into K
consecutive time windows and evaluates the (fixed) Oracle params on each one.

What it tells us that one split can't:
  - Is accuracy stable across market regimes, or did one lucky window carry it?
  - Is the skip rate exploding in recent data (vol-filter curve fitting)?
  - Is the per-class edge (e.g. UP) consistent, or regime-dependent?

Note: parameters are NOT re-tuned per fold. This is validation of a fixed
configuration across time — the honest question for "would this have kept
working?" Re-tuning per fold would test the *process*, which is a later,
more expensive exercise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest import run_oracle_backtest, confidence_buckets
from .classifier import OracleStrategy

LOGGER = logging.getLogger("infiniteloop.oracle.walkforward")

DEFAULT_FOLDS = 6
MIN_FOLD_TRADE_DAYS = 15      # below this a fold's accuracy is statistically meaningless
STABLE_ACCURACY_STD = 0.08    # fold-to-fold std above this = regime-fragile


@dataclass
class WalkForwardReport:
    folds: list[dict] = field(default_factory=list)
    mean_accuracy: float = 0.0
    std_accuracy: float = 0.0
    min_accuracy: float = 0.0
    max_accuracy: float = 0.0
    mean_macro: float = 0.0
    total_trade_days: int = 0
    thin_folds: int = 0           # folds with too few trade days to score
    verdict: str = ""
    confidence_buckets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "folds": self.folds,
            "mean_accuracy": round(self.mean_accuracy, 4),
            "std_accuracy": round(self.std_accuracy, 4),
            "min_accuracy": round(self.min_accuracy, 4),
            "max_accuracy": round(self.max_accuracy, 4),
            "mean_macro": round(self.mean_macro, 4),
            "total_trade_days": self.total_trade_days,
            "thin_folds": self.thin_folds,
            "verdict": self.verdict,
            "confidence_buckets": self.confidence_buckets,
        }


def run_walk_forward(
    features: pd.DataFrame,
    strategy: OracleStrategy,
    n_folds: int = DEFAULT_FOLDS,
    outcome_col: str = "outcome",
) -> WalkForwardReport:
    """
    Evaluate fixed Oracle params across n_folds consecutive time windows
    spanning the FULL feature history (chronological order).
    """
    report = WalkForwardReport()
    if features is None or features.empty or len(features) < n_folds * 20:
        report.verdict = "insufficient_data"
        return report

    segments = np.array_split(np.arange(len(features)), n_folds)
    fold_accuracies: list[float] = []
    fold_macros: list[float] = []
    all_raw: list[pd.DataFrame] = []

    for fold_num, seg in enumerate(segments, start=1):
        window = features.iloc[seg[0]: seg[-1] + 1]
        results = run_oracle_backtest(window, strategy, outcome_col)
        macro = (results.up_accuracy + results.down_accuracy + results.neutral_accuracy) / 3.0
        thin = results.trade_days < MIN_FOLD_TRADE_DAYS

        report.folds.append({
            "fold": fold_num,
            "start": str(window.index.min().date() if hasattr(window.index.min(), "date") else window.index.min()),
            "end": str(window.index.max().date() if hasattr(window.index.max(), "date") else window.index.max()),
            "total_days": results.total_days,
            "trade_days": results.trade_days,
            "skip_rate": round(results.skip_rate, 4),
            "skip_reasons": results.skip_reasons,
            "accuracy": round(results.overall_accuracy, 4),
            "macro": round(macro, 4),
            "up": round(results.up_accuracy, 4),
            "down": round(results.down_accuracy, 4),
            "neutral": round(results.neutral_accuracy, 4),
            "thin": thin,
        })

        if thin:
            report.thin_folds += 1
        else:
            fold_accuracies.append(results.overall_accuracy)
            fold_macros.append(macro)
        report.total_trade_days += results.trade_days
        if results.raw is not None and not results.raw.empty:
            all_raw.append(results.raw)

    if fold_accuracies:
        report.mean_accuracy = float(np.mean(fold_accuracies))
        report.std_accuracy = float(np.std(fold_accuracies))
        report.min_accuracy = float(np.min(fold_accuracies))
        report.max_accuracy = float(np.max(fold_accuracies))
        report.mean_macro = float(np.mean(fold_macros))

    # Confidence buckets across ALL folds combined — the trade agent's map
    if all_raw:
        report.confidence_buckets = confidence_buckets(pd.concat(all_raw))

    # Verdict
    if not fold_accuracies:
        report.verdict = "all_folds_thin"
    elif report.thin_folds > n_folds // 2:
        report.verdict = "mostly_thin_folds_check_skip_reasons"
    elif report.std_accuracy > STABLE_ACCURACY_STD:
        report.verdict = "unstable_across_regimes"
    elif report.min_accuracy < 0.34:
        report.verdict = "at_least_one_fold_at_chance"
    else:
        report.verdict = "stable"

    LOGGER.info(
        "Walk-forward: mean=%.2f%% std=%.2f%% min=%.2f%% max=%.2f%% | thin_folds=%d | verdict=%s",
        report.mean_accuracy * 100, report.std_accuracy * 100,
        report.min_accuracy * 100, report.max_accuracy * 100,
        report.thin_folds, report.verdict,
    )
    return report

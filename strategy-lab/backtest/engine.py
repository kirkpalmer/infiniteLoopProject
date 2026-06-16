"""Direction model backtester. Given a set of day-feature rows and
a BaseStrategy, computes direction accuracy metrics. This is separate from spread P&L -
first we measure how well the classifier works, then spread_engine applies the spreads."""

from __future__ import annotations

import logging

import pandas as pd

LOGGER = logging.getLogger("infiniteloop.backtest.engine")


def backtest_direction(features_df: pd.DataFrame, strategy) -> pd.DataFrame:
    """Run direction classification over a feature frame."""

    rows: list[dict[str, object]] = []
    for date, row in features_df.iterrows():
        predicted = strategy.classify_direction(row)
        actual = row["day_outcome"]
        rows.append(
            {
                "date": pd.Timestamp(date),
                "predicted": predicted,
                "actual": actual,
                "correct": bool(predicted == actual),
            }
        )
    result = pd.DataFrame(rows)
    LOGGER.info("Backtested direction on %d rows", len(result))
    return result


def direction_accuracy(results_df: pd.DataFrame) -> dict[str, float]:
    """Compute overall and per-class accuracy metrics."""

    if results_df.empty:
        return {"accuracy": 0.0, "up_accuracy": 0.0, "down_accuracy": 0.0, "neutral_accuracy": 0.0, "skip_rate": 1.0}

    accuracy = float(results_df["correct"].mean())
    class_metrics = {}
    for label in ["UP", "DOWN", "NEUTRAL"]:
        class_frame = results_df.loc[results_df["actual"] == label]
        class_metrics[f"{label.lower()}_accuracy"] = float(class_frame["correct"].mean()) if not class_frame.empty else 0.0
    skip_rate = float((results_df["predicted"] == "SKIP").mean())
    return {"accuracy": accuracy, **class_metrics, "skip_rate": skip_rate}

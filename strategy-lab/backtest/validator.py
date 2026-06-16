"""Out-of-sample and walk-forward validation for 0DTE spread strategies.
The OOS set is the final arbiter - if a strategy doesn't perform OOS, it is rejected."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from constants import WALK_FORWARD_FOLDS
from .engine import backtest_direction
from .metrics import StrategyScorecard, score_results
from .spread_engine import backtest_spreads

LOGGER = logging.getLogger("infiniteloop.backtest.validator")


@dataclass
class ValidationResult:
    strategy_name: str
    in_sample_scorecard: StrategyScorecard
    oos_scorecard: StrategyScorecard
    walk_forward_scorecards: list[StrategyScorecard]
    oos_degradation_pct: float
    walk_forward_consistency: float
    final_verdict: bool
    rejection_reason: str = ""

    def summary_str(self) -> str:
        return (
            f"strategy={self.strategy_name} verdict={self.final_verdict} "
            f"oos_deg={self.oos_degradation_pct:.2%} wf_consistency={self.walk_forward_consistency:.2%} "
            f"reason={self.rejection_reason}"
        )


class Validator:
    def __init__(self, direction_engine, spread_engine, spx_daily: pd.DataFrame, vix_df: pd.DataFrame, risk_free_rate: float = 0.05) -> None:
        self.direction_engine = direction_engine
        self.spread_engine = spread_engine
        self.spx_daily = spx_daily
        self.vix_df = vix_df
        self.risk_free_rate = risk_free_rate

    def _score(self, features_df: pd.DataFrame, strategy) -> tuple[pd.DataFrame, pd.DataFrame, StrategyScorecard]:
        direction = backtest_direction(features_df, strategy)
        spreads = backtest_spreads(direction, self.spx_daily, self.vix_df, strategy.get_spread_params(), self.risk_free_rate)
        scorecard = score_results(direction, spreads)
        return direction, spreads, scorecard

    def validate(self, train_df: pd.DataFrame, oos_df: pd.DataFrame, strategy, strategy_name: str) -> ValidationResult:
        in_direction, in_spreads, in_sample_scorecard = self._score(train_df, strategy)
        oos_direction, oos_spreads, oos_scorecard = self._score(oos_df, strategy)

        fold_size = max(len(train_df) // WALK_FORWARD_FOLDS, 1)
        walk_forward_scorecards: list[StrategyScorecard] = []
        for fold in range(WALK_FORWARD_FOLDS):
            fold_start = fold * fold_size
            fold_end = len(train_df) if fold == WALK_FORWARD_FOLDS - 1 else (fold + 1) * fold_size
            if fold_start >= len(train_df):
                break
            fold_df = train_df.iloc[:fold_end]
            _, _, scorecard = self._score(fold_df, strategy)
            walk_forward_scorecards.append(scorecard)

        oos_degradation_pct = (in_sample_scorecard.direction_accuracy - oos_scorecard.direction_accuracy) / (in_sample_scorecard.direction_accuracy + 1e-9)
        walk_forward_consistency = sum(scorecard.passed for scorecard in walk_forward_scorecards) / max(len(walk_forward_scorecards), 1)

        final_verdict = bool(
            oos_scorecard.passed
            and oos_degradation_pct < 0.30
            and walk_forward_consistency >= 0.60
        )
        rejection_reason = ""
        if not final_verdict:
            if not oos_scorecard.passed:
                rejection_reason = "oos_failed"
            elif oos_degradation_pct >= 0.30:
                rejection_reason = "excessive_oos_degradation"
            elif walk_forward_consistency < 0.60:
                rejection_reason = "walk_forward_inconsistent"

        result = ValidationResult(
            strategy_name=strategy_name,
            in_sample_scorecard=in_sample_scorecard,
            oos_scorecard=oos_scorecard,
            walk_forward_scorecards=walk_forward_scorecards,
            oos_degradation_pct=float(oos_degradation_pct),
            walk_forward_consistency=float(walk_forward_consistency),
            final_verdict=final_verdict,
            rejection_reason=rejection_reason,
        )
        LOGGER.info(result.summary_str())
        return result

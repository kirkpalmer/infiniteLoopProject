"""
Oracle — InfiniteLoop's Direction Agent.

Oracle classifies each 0DTE trading day as UP, DOWN, NEUTRAL, or SKIP
and returns a confidence score + per-class scores for Sigma to use.
"""

from .classifier import OracleStrategy, OracleParams, OracleSignal, ORACLE_OPTIMIZABLE_PARAMS
from .outcomes import label_outcomes, compute_expected_move, merge_outcomes_into_features
from .backtest import run_oracle_backtest, OracleResults, run_oracle_oos_backtest

__all__ = [
    "OracleStrategy",
    "OracleParams",
    "OracleSignal",
    "ORACLE_OPTIMIZABLE_PARAMS",
    "label_outcomes",
    "compute_expected_move",
    "merge_outcomes_into_features",
    "run_oracle_backtest",
    "run_oracle_oos_backtest",
    "OracleResults",
]

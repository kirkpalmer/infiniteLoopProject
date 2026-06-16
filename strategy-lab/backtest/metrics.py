"""Scorecard for 0DTE spread strategy performance. Combines
direction model accuracy with spread P&L metrics into a single evaluation object."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from constants import MAX_DRAWDOWN_PCT, MIN_DIRECTION_ACCURACY, MIN_PROFIT_FACTOR, MIN_SHARPE, MIN_TRADE_COUNT
from .engine import direction_accuracy

LOGGER = logging.getLogger("infiniteloop.backtest.metrics")


@dataclass
class StrategyScorecard:
    total_days: int
    direction_accuracy: float
    up_accuracy: float
    down_accuracy: float
    neutral_accuracy: float
    skip_rate: float
    total_trades: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown_pct: float
    total_return_pct: float
    expectancy_dollars: float
    avg_win_dollars: float
    avg_loss_dollars: float
    passed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_str(self) -> str:
        return (
            f"accuracy={self.direction_accuracy:.3f} pf={self.profit_factor:.2f} sharpe={self.sharpe_ratio:.2f} "
            f"dd={self.max_drawdown_pct:.2%} trades={self.total_trades} win_rate={self.win_rate:.2%} passed={self.passed}"
        )


def _sharpe_ratio(pnl: pd.Series) -> float:
    if pnl.empty or pnl.std(ddof=0) == 0:
        return 0.0
    return float(np.sqrt(252) * pnl.mean() / pnl.std(ddof=0))


def _max_drawdown(cumulative: pd.Series) -> float:
    if cumulative.empty:
        return 0.0
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max.replace(0, np.nan)
    return float(drawdown.min()) if not drawdown.empty else 0.0


def score_results(direction_results: pd.DataFrame, spread_results: pd.DataFrame, initial_equity: float = 5000.0) -> StrategyScorecard:
    """Combine direction and spread metrics into one scorecard."""

    direction_metrics = direction_accuracy(direction_results)
    pnl = spread_results.get("pnl_per_contract", pd.Series(dtype=float)).fillna(0.0).astype(float)
    trades = spread_results.loc[spread_results.get("trade_type", pd.Series(dtype=str)) != "skipped"].copy()

    total_trades = int(len(trades))
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    win_rate = float((pnl > 0).mean()) if not pnl.empty else 0.0
    avg_win = float(wins.mean()) if not wins.empty else 0.0
    avg_loss = float(losses.mean()) if not losses.empty else 0.0
    expectancy = float(pnl.mean()) if not pnl.empty else 0.0
    cumulative_pnl = pnl.cumsum()
    equity_curve = initial_equity + cumulative_pnl
    max_drawdown_pct = abs(_max_drawdown(equity_curve / initial_equity))
    total_return_pct = float(cumulative_pnl.sum() / initial_equity) if initial_equity else 0.0
    sharpe = _sharpe_ratio(pnl)

    passed = all(
        [
            direction_metrics["accuracy"] >= MIN_DIRECTION_ACCURACY,
            total_trades >= MIN_TRADE_COUNT,
            profit_factor >= MIN_PROFIT_FACTOR,
            sharpe >= MIN_SHARPE,
            max_drawdown_pct <= MAX_DRAWDOWN_PCT,
        ]
    )

    scorecard = StrategyScorecard(
        total_days=int(len(direction_results)),
        direction_accuracy=direction_metrics["accuracy"],
        up_accuracy=direction_metrics["up_accuracy"],
        down_accuracy=direction_metrics["down_accuracy"],
        neutral_accuracy=direction_metrics["neutral_accuracy"],
        skip_rate=direction_metrics["skip_rate"],
        total_trades=total_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        total_return_pct=total_return_pct,
        expectancy_dollars=expectancy,
        avg_win_dollars=avg_win,
        avg_loss_dollars=avg_loss,
        passed=passed,
    )
    LOGGER.info(scorecard.summary_str())
    return scorecard

"""Rich terminal dashboard for InfiniteLoop backtest results.
Called after every backtest run to display scorecard, trade table, equity curve,
and Hermes iteration history. Also exports an HTML snapshot for archiving."""

from __future__ import annotations

import base64
import io
import logging
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from rich.console import Console
from rich.table import Table

from backtest.metrics import StrategyScorecard

LOGGER = logging.getLogger("infiniteloop.dashboard")
CONSOLE = Console()


def _status(ok: bool) -> str:
    return "✅" if ok else "❌"


def render(scorecard: StrategyScorecard, spread_results: pd.DataFrame, direction_results: pd.DataFrame, strategy_params: dict, iteration: int, tier: int, history: list[dict]) -> None:
    """Render a compact but informative terminal dashboard."""

    CONSOLE.rule(f"InfiniteLoop Strategy Lab | Iteration {iteration} | Tier {tier}")
    CONSOLE.print(f"Strategy params: {strategy_params}")

    score_table = Table(title="Scorecard")
    score_table.add_column("Metric")
    score_table.add_column("Value")
    score_table.add_column("Threshold")
    score_table.add_column("Status")
    score_table.add_row("Direction Accuracy", f"{scorecard.direction_accuracy:.2%}", "> 55%", _status(scorecard.direction_accuracy >= 0.55))
    score_table.add_row("Win Rate", f"{scorecard.win_rate:.2%}", "> 50%", "-")
    score_table.add_row("Profit Factor", f"{scorecard.profit_factor:.2f}", "> 1.5", _status(scorecard.profit_factor >= 1.5))
    score_table.add_row("Sharpe Ratio", f"{scorecard.sharpe_ratio:.2f}", "> 0.8", _status(scorecard.sharpe_ratio >= 0.8))
    score_table.add_row("Max Drawdown", f"{scorecard.max_drawdown_pct:.2%}", "< 20%", _status(scorecard.max_drawdown_pct <= 0.20))
    score_table.add_row("Total Trades", str(scorecard.total_trades), "≥ 200", _status(scorecard.total_trades >= 200))
    score_table.add_row("Expectancy", f"${scorecard.expectancy_dollars:.2f}", "> 0", _status(scorecard.expectancy_dollars > 0))
    score_table.add_row("Avg Win / Avg Loss", f"${scorecard.avg_win_dollars:.2f} / ${scorecard.avg_loss_dollars:.2f}", "—", "-")
    score_table.add_row("Skip Rate", f"{scorecard.skip_rate:.2%}", "—", "-")
    CONSOLE.print(score_table)

    trade_table = Table(title="Last 10 Trades")
    for column in ["Date", "Type", "Direction", "Correct?", "Credit", "Exit $", "P&L", "Reason"]:
        trade_table.add_column(column)
    for _, row in spread_results.tail(10).iterrows():
        trade_table.add_row(
            str(row.get("date", ""))[:10],
            str(row.get("trade_type", "")),
            str(row.get("direction_signal", "")),
            _status(bool(row.get("direction_correct", False))),
            f"{float(row.get('credit_received', 0.0)):.2f}",
            f"{float(row.get('exit_price', 0.0)):.2f}",
            f"{float(row.get('pnl_per_contract', 0.0)):.2f}",
            str(row.get("exit_reason", "")),
        )
    CONSOLE.print(trade_table)

    history_table = Table(title="Hermes Iteration History")
    for column in ["Iter", "Tier", "Changed", "Old → New", "Sharpe Δ", "PF Δ"]:
        history_table.add_column(column)
    for item in history[-5:]:
        history_table.add_row(str(item.get("iter", "")), str(item.get("tier", "")), str(item.get("changed", "")), str(item.get("old_new", "")), str(item.get("sharpe_delta", "")), str(item.get("pf_delta", "")))
    CONSOLE.print(history_table)


def export_html(scorecard: StrategyScorecard, spread_results: pd.DataFrame, history: list[dict], output_path: str) -> None:
    """Export a standalone HTML snapshot of the current backtest."""

    cumulative = spread_results.get("pnl_per_contract", pd.Series(dtype=float)).fillna(0.0).astype(float).cumsum()
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.plot(cumulative.index, cumulative.values)
    ax.set_title("Equity Curve")
    ax.set_xlabel("Trade")
    ax.set_ylabel("Cumulative P&L")
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    image_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    html = f"""
    <html>
      <body>
        <h1>InfiniteLoop Strategy Lab</h1>
        <pre>{scorecard.summary_str()}</pre>
        <img src='data:image/png;base64,{image_b64}' />
        {spread_results.tail(50).to_html(index=False)}
        <pre>{history}</pre>
      </body>
    </html>
    """
    Path(output_path).write_text(html, encoding="utf-8")

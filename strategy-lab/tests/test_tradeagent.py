"""
tests/test_tradeagent.py — Path-based spread simulator tests.

Verifies the things the old broken simulator got wrong:
  - exits depend on the ACTUAL price path
  - hard risk sizing is enforced in code
  - confidence controls strike distance via EM multiples
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradeagent.simulator import (
    TradeParams,
    contracts_for_trade,
    expected_move,
    run_trade_backtest,
    simulate_day,
    strike_distance_em_mult,
)

SPX_OPEN = 5000.0
VIX = 20.0


def _path(end_offset: float, n: int = 390, dip: float | None = None) -> pd.Series:
    """Linear intraday path from open to open+end_offset, optional midday dip."""
    base = np.linspace(SPX_OPEN, SPX_OPEN + end_offset, n)
    if dip is not None:
        mid = n // 2
        base[mid - 20: mid + 20] += dip
    return pd.Series(base)


def _params(**kw) -> TradeParams:
    # 5-wide: the only width whose 1-contract max loss fits the 10% rule at $5k
    defaults = dict(em_mult_high=0.5, em_mult_low=1.2, spread_width=5.0,
                    profit_target_pct=50.0, stop_loss_pct=200.0, entry_minute=50)
    defaults.update(kw)
    return TradeParams(**defaults)


def test_strike_distance_scales_with_confidence():
    p = _params()
    assert strike_distance_em_mult(1.0, p) == pytest.approx(0.5)
    assert strike_distance_em_mult(0.5, p) == pytest.approx(1.2)
    mid = strike_distance_em_mult(0.75, p)
    assert 0.5 < mid < 1.2


def test_flat_up_day_wins_bull_put():
    """UP call + flat/up day -> put spread decays -> profit (target or forced)."""
    rec = simulate_day("2026-01-05", "UP", 0.8, SPX_OPEN, VIX,
                       _path(+20), _params(), equity=5000.0)
    assert rec.trade_type == "bull_put_spread"
    assert rec.exit_reason in ("profit_target", "forced_exit")
    assert rec.pnl > 0
    assert rec.contracts == 1          # $5k equity -> hard 1-contract rule


def test_crash_day_stops_out_bull_put():
    """UP call but market collapses through the strikes -> stop loss, bounded loss."""
    em = expected_move(SPX_OPEN, VIX)
    crash = -(em * 2.5)                # far through any reasonable short strike
    rec = simulate_day("2026-01-06", "UP", 0.9, SPX_OPEN, VIX,
                       _path(crash), _params(), equity=5000.0)
    assert rec.trade_type == "bull_put_spread"
    assert rec.exit_reason in ("stop_loss", "forced_exit")
    assert rec.pnl < 0
    # Defined risk: loss can never exceed max_loss * 100 * contracts + commission
    assert abs(rec.pnl) <= rec.max_loss_points * 100 * rec.contracts + 5.0 + 1e-6


def test_same_day_different_path_different_outcome():
    """The old simulator returned profit regardless of path — assert that's dead."""
    p = _params()
    win = simulate_day("2026-01-07", "UP", 0.9, SPX_OPEN, VIX, _path(+15), p, 5000.0)
    em = expected_move(SPX_OPEN, VIX)
    loss = simulate_day("2026-01-07", "UP", 0.9, SPX_OPEN, VIX, _path(-(em * 2.5)), p, 5000.0)
    assert win.pnl > 0 > loss.pnl


def test_down_call_sells_call_spread():
    em = expected_move(SPX_OPEN, VIX)
    rec = simulate_day("2026-01-08", "DOWN", 0.8, SPX_OPEN, VIX,
                       _path(-15), _params(), equity=5000.0)
    assert rec.trade_type == "bear_call_spread"
    assert rec.short_strike > SPX_OPEN     # OTM call above spot
    assert rec.pnl > 0


def test_hard_sizing_rules():
    # One contract of a 10-wide (~$1000 risk) exceeds 10% of $5k? No: budget=$500 < ~$900 max loss -> skip
    assert contracts_for_trade(equity=5000.0, max_loss_points=9.0) == 0
    # $5k with a 4-point max loss ($400 <= $500 budget) -> exactly 1 (single-contract cap)
    assert contracts_for_trade(equity=5000.0, max_loss_points=4.0) == 1
    # $14k -> still capped at 1 regardless of budget
    assert contracts_for_trade(equity=14000.0, max_loss_points=4.0) == 1
    # $40k, $400 risk per contract, $4k budget -> 10 contracts
    assert contracts_for_trade(equity=40000.0, max_loss_points=4.0) == 10


def test_wide_spread_skipped_on_small_account():
    """A spread whose 1-contract max loss exceeds 10% of equity must be skipped."""
    rec = simulate_day("2026-01-09", "UP", 0.9, SPX_OPEN, VIX,
                       _path(+10), _params(spread_width=25.0), equity=5000.0)
    assert rec.trade_type == "no_trade"
    assert rec.exit_reason == "skipped_risk_budget"


def test_condor_flat_day_wins():
    """Range-bound day -> condor collects premium."""
    from tradeagent.simulator import simulate_condor_day
    rec = simulate_condor_day("2026-01-12", 0.4, SPX_OPEN, VIX,
                              _path(+5), _params(spread_width=5.0), equity=5000.0)
    assert rec.trade_type == "iron_condor"
    assert rec.exit_reason in ("profit_target", "forced_exit")
    assert rec.pnl > 0


def test_condor_trend_day_loses_bounded():
    """Big directional day blows through one wing -> bounded loss."""
    from tradeagent.simulator import simulate_condor_day, expected_move
    em = expected_move(SPX_OPEN, VIX)
    rec = simulate_condor_day("2026-01-13", 0.4, SPX_OPEN, VIX,
                              _path(-(em * 3)), _params(spread_width=5.0), equity=5000.0)
    assert rec.trade_type == "iron_condor"
    assert rec.pnl < 0
    assert abs(rec.pnl) <= rec.max_loss_points * 100 * rec.contracts + 10.0 + 1e-6


def test_choose_trade_policy_mapping():
    from tradeagent.simulator import choose_trade
    # Directional calls trade verticals under BOTH policies
    assert choose_trade("directional_only", "UP") == "vertical"
    assert choose_trade("always_in", "DOWN") == "vertical"
    # Ambiguous days: stand aside vs condor
    assert choose_trade("directional_only", "NEUTRAL") == "none"
    assert choose_trade("directional_only", "SKIP", "low_confidence") == "none"
    assert choose_trade("always_in", "NEUTRAL") == "condor"
    assert choose_trade("always_in", "SKIP", "low_confidence") == "condor"
    # Hard skips sit out even under always_in
    assert choose_trade("always_in", "SKIP", "vix_above_high") == "none"
    assert choose_trade("always_in", "SKIP", "missing_features") == "none"


def test_policy_race_runs_both():
    from tradeagent.simulator import compare_policies
    days = pd.DataFrame({
        "date": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        "call": ["UP", "NEUTRAL", "SKIP", "DOWN"],
        "confidence": [0.8, 0.4, 0.0, 0.7],
        "skip_reason": ["", "", "low_confidence", ""],
        "spx_open": [SPX_OPEN] * 4,
        "vix": [VIX] * 4,
    })
    paths = {d: _path(+5) for d in days["date"]}
    out = compare_policies(days, lambda d: paths.get(d), _params(spread_width=5.0))
    a = out["policies"]["directional_only"]
    b = out["policies"]["always_in"]
    assert a["n_trades"] == 2            # UP + DOWN only
    assert b["n_trades"] == 4            # + condors on NEUTRAL and low-conf SKIP
    assert "iron_condor" in b["by_trade_type"]
    assert len(b["equity_curve"]) == 5


def test_portfolio_backtest_compounds_and_stands_aside():
    days = pd.DataFrame({
        "date": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        "call": ["UP", "NEUTRAL", "DOWN", "SKIP"],
        "confidence": [0.8, 0.6, 0.7, 0.0],
        "spx_open": [SPX_OPEN] * 4,
        "vix": [VIX] * 4,
    })
    paths = {
        "2026-01-05": _path(+15),
        "2026-01-07": _path(-12),
    }
    result = run_trade_backtest(days, lambda d: paths.get(d), _params(spread_width=5.0))
    assert result.n_trades == 2                 # NEUTRAL and SKIP stood aside
    assert result.final_equity != result.starting_equity
    assert len(result.equity_curve) == 3        # start + 2 executed trades
    s = result.summary_dict()
    assert set(s) >= {"n_trades", "win_rate", "expectancy", "profit_factor",
                      "max_drawdown_pct", "final_equity"}

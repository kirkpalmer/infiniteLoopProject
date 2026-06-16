"""Spread P&L backtester for InfiniteLoop Phase 1. Given direction results
and a strategy's spread params, simulates each day's spread trade using synthetic
Black-Scholes pricing (VIX as IV proxy). Returns a trade-by-trade P&L DataFrame."""

from __future__ import annotations

import logging

import pandas as pd

from data.options import simulate_iron_condor, simulate_spread

LOGGER = logging.getLogger("infiniteloop.backtest.spread_engine")


def backtest_spreads(
    direction_results: pd.DataFrame,
    spx_daily: pd.DataFrame,
    vix_df: pd.DataFrame,
    spread_params: dict,
    risk_free_rate: float = 0.05,
) -> pd.DataFrame:
    """Simulate the spread trade for each classified day."""

    rows: list[dict[str, object]] = []
    for _, row in direction_results.iterrows():
        date = pd.Timestamp(row["date"]).normalize()
        predicted = str(row["predicted"])
        actual = str(row["actual"])
        direction_correct = bool(predicted == actual)

        if predicted == "SKIP":
            rows.append(
                {
                    "date": date,
                    "trade_type": "skipped",
                    "direction_signal": predicted,
                    "direction_correct": direction_correct,
                    "short_strike": None,
                    "long_strike": None,
                    "credit_received": 0.0,
                    "max_loss": 0.0,
                    "exit_price": 0.0,
                    "pnl_per_contract": 0.0,
                    "exit_reason": "skipped",
                }
            )
            continue

        if date not in spx_daily.index or date not in vix_df.index:
            LOGGER.warning("Missing SPX or VIX data for %s; skipping trade", date.date())
            rows.append(
                {
                    "date": date,
                    "trade_type": "skipped",
                    "direction_signal": predicted,
                    "direction_correct": direction_correct,
                    "short_strike": None,
                    "long_strike": None,
                    "credit_received": 0.0,
                    "max_loss": 0.0,
                    "exit_price": 0.0,
                    "pnl_per_contract": 0.0,
                    "exit_reason": "skipped",
                }
            )
            continue

        spot_price = float(spx_daily.loc[date, "open"])
        vix = float(vix_df.loc[date, "vix_close"])
        if predicted == "NEUTRAL":
            result = simulate_iron_condor(date, spot_price, vix, spread_params, risk_free_rate=risk_free_rate, direction_correct=direction_correct)
        else:
            result = simulate_spread(date, predicted, spot_price, vix, spread_params, risk_free_rate=risk_free_rate, direction_correct=direction_correct)
        rows.append(result.__dict__)

    frame = pd.DataFrame(rows)
    LOGGER.info("Backtested spreads on %d rows", len(frame))
    return frame

"""Synthetic options pricing and spread P&L calculator for InfiniteLoop.
Uses Black-Scholes (py_vollib) with VIX as the implied volatility proxy.
No paid options chain data required - all pricing is computed locally."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite
from typing import Literal

import pandas as pd
from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta

from constants import DEFAULT_PROFIT_TARGET_PCT, DEFAULT_SHORT_DELTA, DEFAULT_SPREAD_WIDTH_USD, DEFAULT_STOP_LOSS_PCT, SPX_MULTIPLIER

LOGGER = logging.getLogger("infiniteloop.data.options")

VIX_0DTE_SCALING = 1.15
MIN_CREDIT_THRESHOLD = 0.10


@dataclass
class SpreadResult:
    date: pd.Timestamp
    trade_type: str
    direction_signal: str
    direction_correct: bool
    short_strike: float | None
    long_strike: float | None
    credit_received: float
    max_loss: float
    exit_price: float
    pnl_per_contract: float
    exit_reason: str


def sigma_from_vix(vix: float, scaling: float = VIX_0DTE_SCALING) -> float:
    """Convert VIX level to annualized sigma for Black-Scholes."""

    return max((vix / 100.0) * scaling, 1e-6)


def time_to_expiry(entry_hour: int, entry_minute: int = 0, market_close_hour: int = 16) -> float:
    """Return fraction of year remaining from entry time to market close."""

    hours_remaining = max((market_close_hour - entry_hour) - (entry_minute / 60.0), 0.0)
    return hours_remaining / 8760.0


def find_strike_by_delta(
    spot: float,
    option_type: Literal["p", "c"],
    target_delta: float,
    sigma: float,
    t: float,
    r: float,
) -> float:
    """Binary search a strike whose absolute Black-Scholes delta matches the target."""

    strikes = [round(spot * (1 - 0.2) + step * 0.25, 2) for step in range(int((spot * 0.4) / 0.25) + 1)]
    best_strike = strikes[0]
    best_error = float("inf")

    for strike in strikes:
        try:
            option_delta = abs(float(bs_delta(option_type, spot, strike, t, r, sigma)))
        except Exception:
            continue
        error = abs(option_delta - target_delta)
        if error < best_error:
            best_error = error
            best_strike = strike

    if not isfinite(best_strike):
        raise ValueError("Could not find a strike matching target delta")
    return float(best_strike)


def price_spread(
    spot: float,
    short_strike: float,
    long_strike: float,
    option_type: Literal["p", "c"],
    sigma: float,
    t: float,
    r: float,
) -> float:
    """Price a vertical spread as short leg price minus long leg price."""

    short_price = float(black_scholes(option_type, spot, short_strike, t, r, sigma))
    long_price = float(black_scholes(option_type, spot, long_strike, t, r, sigma))
    return short_price - long_price


def _build_result(
    date: pd.Timestamp,
    trade_type: str,
    direction_signal: str,
    direction_correct: bool,
    short_strike: float | None,
    long_strike: float | None,
    credit: float,
    exit_price: float,
    exit_reason: str,
) -> SpreadResult:
    max_loss = max((short_strike or 0.0) - (long_strike or 0.0), 0.0) - credit if trade_type != "bear_call_spread" else max((long_strike or 0.0) - (short_strike or 0.0), 0.0) - credit
    pnl_per_contract = (credit - exit_price) * SPX_MULTIPLIER
    return SpreadResult(
        date=date,
        trade_type=trade_type,
        direction_signal=direction_signal,
        direction_correct=direction_correct,
        short_strike=short_strike,
        long_strike=long_strike,
        credit_received=credit,
        max_loss=max_loss,
        exit_price=exit_price,
        pnl_per_contract=pnl_per_contract,
        exit_reason=exit_reason,
    )


def simulate_spread(
    date: pd.Timestamp,
    direction: str,
    spot_price: float,
    vix: float,
    spread_params: dict,
    risk_free_rate: float = 0.05,
    direction_correct: bool = False,
) -> SpreadResult:
    """Simulate a directional vertical spread using Black-Scholes pricing."""

    if direction == "SKIP":
        return _build_result(date, "skipped", direction, direction_correct, None, None, 0.0, 0.0, "skipped")

    sigma = sigma_from_vix(vix)
    t_entry = time_to_expiry(int(spread_params.get("entry_hour", 10)))
    target_delta = float(spread_params.get("short_delta", DEFAULT_SHORT_DELTA)) / 100.0
    spread_width = float(spread_params.get("spread_width_usd", DEFAULT_SPREAD_WIDTH_USD))
    profit_target_pct = float(spread_params.get("profit_target_pct", DEFAULT_PROFIT_TARGET_PCT))
    stop_loss_pct = float(spread_params.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT))

    if direction == "UP":
        option_type = "p"
        trade_type = "bull_put_spread"
        short_strike = find_strike_by_delta(spot_price, option_type, target_delta, sigma, t_entry, risk_free_rate)
        long_strike = round(short_strike - spread_width, 2)
    elif direction == "DOWN":
        option_type = "c"
        trade_type = "bear_call_spread"
        short_strike = find_strike_by_delta(spot_price, option_type, target_delta, sigma, t_entry, risk_free_rate)
        long_strike = round(short_strike + spread_width, 2)
    else:
        return simulate_iron_condor(date, spot_price, vix, spread_params, risk_free_rate=risk_free_rate, direction_correct=direction_correct)

    credit = price_spread(spot_price, short_strike, long_strike, option_type, sigma, t_entry, risk_free_rate)
    if credit < MIN_CREDIT_THRESHOLD:
        return _build_result(date, "skipped", direction, direction_correct, short_strike, long_strike, 0.0, 0.0, "skipped")

    profit_exit = credit * (1.0 - profit_target_pct / 100.0)
    stop_exit = credit * (1.0 + stop_loss_pct / 100.0)
    forced_exit = price_spread(spot_price, short_strike, long_strike, option_type, sigma, time_to_expiry(15, 45), risk_free_rate)

    if profit_exit <= credit * 0.99:
        exit_price = profit_exit
        reason = "profit_target"
    elif stop_exit >= credit * 1.01:
        exit_price = stop_exit
        reason = "stop_loss"
    else:
        exit_price = forced_exit
        reason = "forced_exit"

    return _build_result(date, trade_type, direction, direction_correct, short_strike, long_strike, credit, exit_price, reason)


def simulate_iron_condor(
    date: pd.Timestamp,
    spot_price: float,
    vix: float,
    spread_params: dict,
    risk_free_rate: float = 0.05,
    direction_correct: bool = True,
) -> SpreadResult:
    """Simulate an iron condor as combined put and call vertical spreads."""

    sigma = sigma_from_vix(vix)
    t_entry = time_to_expiry(int(spread_params.get("entry_hour", 10)))
    target_delta = float(spread_params.get("short_delta", DEFAULT_SHORT_DELTA)) / 100.0
    spread_width = float(spread_params.get("spread_width_usd", DEFAULT_SPREAD_WIDTH_USD))
    profit_target_pct = float(spread_params.get("profit_target_pct", DEFAULT_PROFIT_TARGET_PCT))
    stop_loss_pct = float(spread_params.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT))

    put_short = find_strike_by_delta(spot_price, "p", target_delta, sigma, t_entry, risk_free_rate)
    put_long = round(put_short - spread_width, 2)
    call_short = find_strike_by_delta(spot_price, "c", target_delta, sigma, t_entry, risk_free_rate)
    call_long = round(call_short + spread_width, 2)

    put_credit = price_spread(spot_price, put_short, put_long, "p", sigma, t_entry, risk_free_rate)
    call_credit = price_spread(spot_price, call_short, call_long, "c", sigma, t_entry, risk_free_rate)
    credit = put_credit + call_credit
    if credit < MIN_CREDIT_THRESHOLD:
        return _build_result(date, "skipped", "NEUTRAL", direction_correct, None, None, 0.0, 0.0, "skipped")

    exit_price = credit * (1.0 - profit_target_pct / 100.0)
    if exit_price <= credit * 0.99:
        reason = "profit_target"
    elif credit * (1.0 + stop_loss_pct / 100.0) >= credit * 1.01:
        exit_price = credit * (1.0 + stop_loss_pct / 100.0)
        reason = "stop_loss"
    else:
        reason = "forced_exit"
        exit_price = credit * 0.75

    return _build_result(date, "iron_condor", "NEUTRAL", direction_correct, put_short, put_long, credit, exit_price, reason)

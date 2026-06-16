"""
tradeagent/simulator.py — Path-based 0DTE vertical spread simulator.

Unlike the retired data/options.simulate_spread() (which never consulted the
price path and marked every trade a winner), this simulator walks each trade
through the day's actual intraday bars:

  1. Enter at entry_minute after the open (after Oracle has classified).
  2. Reprice the spread every PRICE_STEP_MINUTES via Black-Scholes
     (VIX-derived sigma, shrinking time-to-expiry).
  3. Exit on stop-loss (checked FIRST — conservative), profit target,
     or the 15:45 forced exit. Never hold to expiration.

HARD RISK RULES enforced in code (never parameters):
  - Defined risk only (verticals).
  - Contracts derived from equity: 1 contract until equity > $15,000, then
    floor(equity * MAX_RISK_PER_TRADE_PCT / max_loss_per_contract).
  - If max loss of ONE contract exceeds the per-trade risk budget, the trade
    is skipped entirely.
  - Forced exit at 15:45 ET.

Known approximation: Black-Scholes with VIX as sigma ignores the volatility
skew, so far-OTM put credits are somewhat optimistic. Parameter sets are
ranked correctly relative to each other; absolute P&L is validated in paper
trading (Phase 2 requirement).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from constants import (
    FORCED_EXIT_HOUR,
    FORCED_EXIT_MINUTE,
    MAX_RISK_PER_TRADE_PCT,
    SPX_MULTIPLIER,
)
from data.options import price_spread, sigma_from_vix

LOGGER = logging.getLogger("infiniteloop.tradeagent.simulator")

# ---------------------------------------------------------------------------
# Simulation constants
# ---------------------------------------------------------------------------
STARTING_EQUITY = 5_000.0
SINGLE_CONTRACT_EQUITY_CAP = 15_000.0   # 1 contract max until equity exceeds this
MAX_CONTRACTS = 25                      # absolute cap — liquidity/slippage realism.
                                        # Uncapped 10%-of-equity compounding produced
                                        # trillion-dollar fantasy curves; nothing real
                                        # scales linearly forever. Revisit in Phase 4.
PRICE_STEP_MINUTES = 5                  # repricing cadence along the intraday path
COMMISSION_PER_SPREAD = 5.0             # round-trip, both legs, per contract (incl. fees)
COMMISSION_PER_CONDOR = 10.0            # four legs round-trip, per contract
STRIKE_INCREMENT = 5.0                  # SPX strikes
RTH_MINUTES = 390                       # 9:30 -> 16:00
TRADING_MINUTES_PER_YEAR = 252 * RTH_MINUTES
MIN_CREDIT = 0.15                       # don't sell spreads for pocket lint
CONF_FLOOR = 0.50                       # confidence at/below this maps to em_mult_low


@dataclass
class TradeParams:
    """Trade-agent parameters — the optimizer's search space (sizing excluded)."""
    em_mult_high: float = 0.5     # strike distance (EM multiples) at confidence 1.0
    em_mult_low: float = 1.2      # strike distance at confidence CONF_FLOOR
    spread_width: float = 5.0     # points between short and long strike ($5-wide fits $5k account @ 10% risk rule)
    profit_target_pct: float = 50.0   # close at this % of max credit captured
    stop_loss_pct: float = 200.0      # close if loss reaches this % of credit
    entry_minute: int = 50        # minutes after open (post-classification)
    condor_em_mult: float = 1.2   # wing distance (EM multiples) for non-directional days

    def to_dict(self) -> dict:
        return {
            "em_mult_high": self.em_mult_high,
            "em_mult_low": self.em_mult_low,
            "spread_width": self.spread_width,
            "profit_target_pct": self.profit_target_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "entry_minute": self.entry_minute,
            "condor_em_mult": self.condor_em_mult,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeParams":
        known = {k: v for k, v in d.items() if k in cls().to_dict()}
        return cls(**known)


@dataclass
class TradeRecord:
    date: str
    direction: str            # UP / DOWN
    confidence: float
    trade_type: str           # bull_put_spread / bear_call_spread / no_trade
    short_strike: float | None
    long_strike: float | None
    credit: float             # per contract, in points
    max_loss_points: float
    contracts: int
    exit_reason: str          # profit_target / stop_loss / forced_exit / skipped_*
    exit_value: float         # spread value at exit, in points
    pnl: float                # dollars, all contracts, net of commission
    equity_after: float


@dataclass
class TradeBacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    starting_equity: float = STARTING_EQUITY
    final_equity: float = STARTING_EQUITY

    # Metrics (computed by finalize())
    n_trades: int = 0
    n_wins: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    expectancy: float = 0.0       # mean $ per trade (compounded sizing)
    expectancy_per_contract: float = 0.0   # mean $ per single contract — the
                                           # unbiased per-trade edge, immune to
                                           # compounding distortion
    profit_factor: float = 0.0
    sharpe: float = 0.0           # per-trade Sharpe (mean/std of trade P&L)
    max_drawdown_pct: float = 0.0
    return_pct: float = 0.0
    exit_reasons: dict = field(default_factory=dict)

    def finalize(self) -> "TradeBacktestResult":
        executed = [t for t in self.trades if t.trade_type != "no_trade"]
        self.n_trades = len(executed)
        pnls = np.array([t.pnl for t in executed], dtype=float)
        if self.n_trades:
            self.n_wins = int((pnls > 0).sum())
            self.win_rate = self.n_wins / self.n_trades
            self.total_pnl = float(pnls.sum())
            self.expectancy = float(pnls.mean())
            per_contract = np.array([t.pnl / t.contracts for t in executed], dtype=float)
            self.expectancy_per_contract = float(per_contract.mean())
            gains = pnls[pnls > 0].sum()
            losses = -pnls[pnls < 0].sum()
            self.profit_factor = float(gains / losses) if losses > 0 else float("inf")
            self.sharpe = float(pnls.mean() / pnls.std()) if pnls.std() > 0 else 0.0
        curve = np.array(self.equity_curve or [self.starting_equity])
        peak = np.maximum.accumulate(curve)
        self.max_drawdown_pct = float(((peak - curve) / peak).max())
        self.final_equity = float(curve[-1])
        self.return_pct = self.final_equity / self.starting_equity - 1.0
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        self.exit_reasons = reasons
        return self

    def summary_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "expectancy": round(self.expectancy, 2),
            "expectancy_per_contract": round(self.expectancy_per_contract, 2),
            "profit_factor": round(self.profit_factor, 3) if math.isfinite(self.profit_factor) else None,
            "sharpe": round(self.sharpe, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "final_equity": round(self.final_equity, 2),
            "return_pct": round(self.return_pct, 4),
            "exit_reasons": self.exit_reasons,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def expected_move(spx_open: float, vix: float) -> float:
    """Market-implied 1-sigma daily range (same formula as Oracle's labels)."""
    return spx_open * (vix / 100.0) * math.sqrt(1.0 / 252.0)


def strike_distance_em_mult(confidence: float, params: TradeParams) -> float:
    """
    Interpolate strike distance (in EM multiples) from Oracle confidence.
    confidence <= CONF_FLOOR -> em_mult_low (furthest strikes)
    confidence >= 1.0        -> em_mult_high (tightest strikes)
    """
    span = max(1.0 - CONF_FLOOR, 1e-9)
    weight = min(max((confidence - CONF_FLOOR) / span, 0.0), 1.0)
    return params.em_mult_low + (params.em_mult_high - params.em_mult_low) * weight


def _round_strike(value: float) -> float:
    return round(value / STRIKE_INCREMENT) * STRIKE_INCREMENT


def _minutes_to_close_years(minute_of_day: int) -> float:
    """Time-to-expiry in years from minute-of-RTH (0 = 9:30) to the 16:00 close."""
    remaining = max(RTH_MINUTES - minute_of_day, 1)
    return remaining / TRADING_MINUTES_PER_YEAR


def contracts_for_trade(equity: float, max_loss_points: float) -> int:
    """
    HARD RISK RULE — sizing is derived, never optimized.
      - max risk per trade: MAX_RISK_PER_TRADE_PCT of current equity
      - 1 contract max until equity > $15,000
      - 0 contracts (skip) if even one contract exceeds the budget
    """
    max_loss_dollars = max_loss_points * SPX_MULTIPLIER
    if max_loss_dollars <= 0:
        return 0
    budget = equity * MAX_RISK_PER_TRADE_PCT
    if max_loss_dollars > budget:
        return 0
    if equity <= SINGLE_CONTRACT_EQUITY_CAP:
        return 1
    return min(max(int(budget // max_loss_dollars), 1), MAX_CONTRACTS)


# ---------------------------------------------------------------------------
# Single-day simulation
# ---------------------------------------------------------------------------

def simulate_day(
    date: str,
    direction: str,           # "UP" or "DOWN" (others -> no_trade)
    confidence: float,
    spx_open: float,
    vix: float,
    intraday_spx: pd.Series,  # 1-min SPX(-scaled) closes indexed 0..N-1 from the open
    params: TradeParams,
    equity: float,
    risk_free_rate: float = 0.04,
) -> TradeRecord:
    """Simulate one day's spread trade along the actual intraday path."""

    def no_trade(reason: str) -> TradeRecord:
        return TradeRecord(
            date=date, direction=direction, confidence=confidence,
            trade_type="no_trade", short_strike=None, long_strike=None,
            credit=0.0, max_loss_points=0.0, contracts=0,
            exit_reason=reason, exit_value=0.0, pnl=0.0, equity_after=equity,
        )

    if direction not in ("UP", "DOWN"):
        return no_trade("skipped_not_directional")
    if intraday_spx is None or len(intraday_spx) <= params.entry_minute + 5:
        return no_trade("skipped_no_intraday_data")

    sigma = sigma_from_vix(vix)
    em = expected_move(spx_open, vix)
    distance = strike_distance_em_mult(confidence, params) * em

    entry_idx = params.entry_minute
    entry_spot = float(intraday_spx.iloc[entry_idx])
    t_entry = _minutes_to_close_years(entry_idx)

    if direction == "UP":
        option_type, trade_type = "p", "bull_put_spread"
        short_strike = _round_strike(entry_spot - distance)
        long_strike = short_strike - params.spread_width
    else:
        option_type, trade_type = "c", "bear_call_spread"
        short_strike = _round_strike(entry_spot + distance)
        long_strike = short_strike + params.spread_width

    credit = price_spread(entry_spot, short_strike, long_strike, option_type, sigma, t_entry, risk_free_rate)
    if credit < MIN_CREDIT:
        return no_trade("skipped_credit_too_small")

    max_loss_points = params.spread_width - credit
    contracts = contracts_for_trade(equity, max_loss_points)
    if contracts == 0:
        return no_trade("skipped_risk_budget")

    profit_exit_value = credit * (1.0 - params.profit_target_pct / 100.0)
    stop_exit_value = credit * (1.0 + params.stop_loss_pct / 100.0)

    forced_idx = (FORCED_EXIT_HOUR - 9) * 60 + FORCED_EXIT_MINUTE - 30  # minute-of-RTH for 15:45
    last_idx = min(forced_idx, len(intraday_spx) - 1)

    exit_value: Optional[float] = None
    exit_reason = "forced_exit"

    for idx in range(entry_idx + PRICE_STEP_MINUTES, last_idx + 1, PRICE_STEP_MINUTES):
        spot = float(intraday_spx.iloc[idx])
        t = _minutes_to_close_years(idx)
        value = price_spread(spot, short_strike, long_strike, option_type, sigma, t, risk_free_rate)
        # Stop checked FIRST (conservative: assume the bad print fills first)
        if value >= stop_exit_value:
            exit_value, exit_reason = min(value, params.spread_width), "stop_loss"
            break
        if value <= profit_exit_value:
            exit_value, exit_reason = max(value, 0.0), "profit_target"
            break

    if exit_value is None:
        spot = float(intraday_spx.iloc[last_idx])
        t = _minutes_to_close_years(last_idx)
        exit_value = price_spread(spot, short_strike, long_strike, option_type, sigma, t, risk_free_rate)
        exit_value = min(max(exit_value, 0.0), params.spread_width)
        exit_reason = "forced_exit"

    pnl = (credit - exit_value) * SPX_MULTIPLIER * contracts - COMMISSION_PER_SPREAD * contracts

    return TradeRecord(
        date=date, direction=direction, confidence=confidence,
        trade_type=trade_type,
        short_strike=short_strike, long_strike=long_strike,
        credit=round(credit, 3), max_loss_points=round(max_loss_points, 3),
        contracts=contracts,
        exit_reason=exit_reason, exit_value=round(exit_value, 3),
        pnl=round(pnl, 2), equity_after=round(equity + pnl, 2),
    )


def simulate_condor_day(
    date: str,
    confidence: float,
    spx_open: float,
    vix: float,
    intraday_spx: pd.Series,
    params: TradeParams,
    equity: float,
    risk_free_rate: float = 0.04,
) -> TradeRecord:
    """
    Simulate an iron condor on a NON-directional day: sell both a put spread
    and a call spread with wings condor_em_mult x EM away. The bet is RANGE,
    not direction — implied vol overpricing realized vol (Kirk's original
    weekly-trading edge, compressed to 0DTE).
    """
    def no_trade(reason: str) -> TradeRecord:
        return TradeRecord(
            date=date, direction="RANGE", confidence=confidence,
            trade_type="no_trade", short_strike=None, long_strike=None,
            credit=0.0, max_loss_points=0.0, contracts=0,
            exit_reason=reason, exit_value=0.0, pnl=0.0, equity_after=equity,
        )

    if intraday_spx is None or len(intraday_spx) <= params.entry_minute + 5:
        return no_trade("skipped_no_intraday_data")

    sigma = sigma_from_vix(vix)
    em = expected_move(spx_open, vix)
    distance = params.condor_em_mult * em

    entry_idx = params.entry_minute
    entry_spot = float(intraday_spx.iloc[entry_idx])
    t_entry = _minutes_to_close_years(entry_idx)

    put_short = _round_strike(entry_spot - distance)
    put_long = put_short - params.spread_width
    call_short = _round_strike(entry_spot + distance)
    call_long = call_short + params.spread_width

    def condor_value(spot: float, t: float) -> float:
        return (
            price_spread(spot, put_short, put_long, "p", sigma, t, risk_free_rate)
            + price_spread(spot, call_short, call_long, "c", sigma, t, risk_free_rate)
        )

    credit = condor_value(entry_spot, t_entry)
    if credit < MIN_CREDIT:
        return no_trade("skipped_credit_too_small")

    # Only one side can finish in the money -> defined risk = width - credit
    max_loss_points = params.spread_width - credit
    contracts = contracts_for_trade(equity, max_loss_points)
    if contracts == 0:
        return no_trade("skipped_risk_budget")

    profit_exit_value = credit * (1.0 - params.profit_target_pct / 100.0)
    stop_exit_value = credit * (1.0 + params.stop_loss_pct / 100.0)

    forced_idx = (FORCED_EXIT_HOUR - 9) * 60 + FORCED_EXIT_MINUTE - 30
    last_idx = min(forced_idx, len(intraday_spx) - 1)

    exit_value: Optional[float] = None
    exit_reason = "forced_exit"
    for idx in range(entry_idx + PRICE_STEP_MINUTES, last_idx + 1, PRICE_STEP_MINUTES):
        spot = float(intraday_spx.iloc[idx])
        t = _minutes_to_close_years(idx)
        value = condor_value(spot, t)
        if value >= stop_exit_value:                       # stop checked first
            exit_value, exit_reason = min(value, params.spread_width), "stop_loss"
            break
        if value <= profit_exit_value:
            exit_value, exit_reason = max(value, 0.0), "profit_target"
            break

    if exit_value is None:
        spot = float(intraday_spx.iloc[last_idx])
        exit_value = condor_value(spot, _minutes_to_close_years(last_idx))
        exit_value = min(max(exit_value, 0.0), params.spread_width)
        exit_reason = "forced_exit"

    pnl = (credit - exit_value) * SPX_MULTIPLIER * contracts - COMMISSION_PER_CONDOR * contracts

    return TradeRecord(
        date=date, direction="RANGE", confidence=confidence,
        trade_type="iron_condor",
        short_strike=put_short, long_strike=call_short,   # the two short strikes
        credit=round(credit, 3), max_loss_points=round(max_loss_points, 3),
        contracts=contracts,
        exit_reason=exit_reason, exit_value=round(exit_value, 3),
        pnl=round(pnl, 2), equity_after=round(equity + pnl, 2),
    )


# ---------------------------------------------------------------------------
# Trade policies — how the Trade Agent uses Oracle's bias + confidence
# ---------------------------------------------------------------------------
# Oracle's job is INFORMATION (bias + confidence), not trade signals.
# The policy is the Trade Agent's decision rule on top of that information.
#
#   directional_only — trade only UP/DOWN calls; stand aside otherwise.
#   always_in        — UP/DOWN calls -> vertical at confidence-scaled distance;
#                      ambiguous days (NEUTRAL call or low-confidence skip) ->
#                      iron condor at condor_em_mult x EM (range bet);
#                      hard skips only for VIX-regime gates and data gaps.

POLICIES = ("directional_only", "always_in")
HARD_SKIP_REASONS = {"vix_above_high", "vix_below_low", "missing_features"}


def choose_trade(policy: str, call: str, skip_reason: str = "") -> str:
    """Return 'vertical', 'condor', or 'none' for a day's Oracle output."""
    if call in ("UP", "DOWN"):
        return "vertical"
    if policy == "always_in":
        if call == "SKIP" and skip_reason in HARD_SKIP_REASONS:
            return "none"          # unwinnable regime / no data — even always_in sits out
        return "condor"            # NEUTRAL call or low-confidence skip -> range bet
    return "none"


# ---------------------------------------------------------------------------
# Portfolio backtest over Oracle's daily signals
# ---------------------------------------------------------------------------

def run_trade_backtest(
    signal_days: pd.DataFrame,
    bars_provider: Callable[[str], Optional[pd.Series]],
    params: TradeParams,
    policy: str = "directional_only",
    starting_equity: float = STARTING_EQUITY,
    risk_free_rate: float = 0.04,
) -> TradeBacktestResult:
    """
    Compound a portfolio over Oracle's daily bias/confidence output.

    Args:
        signal_days: DataFrame with one row per day, columns:
                     date (str), call (UP/DOWN/NEUTRAL/SKIP), confidence,
                     skip_reason (str), spx_open (float), vix (float).
        bars_provider: callable(date_str) -> 1-min SPX-scaled close Series
                       from the open (or None if no intraday data).
        params: TradeParams under test.
        policy: 'directional_only' or 'always_in' (see POLICIES).
        starting_equity: portfolio starting value (default $5,000).
    """
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy '{policy}' — expected one of {POLICIES}")

    result = TradeBacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    result.equity_curve.append(equity)

    for _, row in signal_days.iterrows():
        date = str(row["date"])
        call = row["call"]
        trade = choose_trade(policy, call, str(row.get("skip_reason", "") or ""))
        if trade == "none":
            continue

        bars = bars_provider(date)
        if trade == "vertical":
            record = simulate_day(
                date=date, direction=call,
                confidence=float(row["confidence"]),
                spx_open=float(row["spx_open"]), vix=float(row["vix"]),
                intraday_spx=bars, params=params, equity=equity,
                risk_free_rate=risk_free_rate,
            )
        else:
            record = simulate_condor_day(
                date=date,
                confidence=float(row["confidence"]),
                spx_open=float(row["spx_open"]), vix=float(row["vix"]),
                intraday_spx=bars, params=params, equity=equity,
                risk_free_rate=risk_free_rate,
            )

        result.trades.append(record)
        if record.trade_type != "no_trade":
            equity = record.equity_after
            result.equity_curve.append(equity)
            if equity <= 0:
                LOGGER.warning("Equity wiped out on %s — halting backtest", date)
                break

    return result.finalize()


def compare_policies(
    signal_days: pd.DataFrame,
    bars_provider: Callable[[str], Optional[pd.Series]],
    params: TradeParams,
    starting_equity: float = STARTING_EQUITY,
    risk_free_rate: float = 0.04,
) -> dict:
    """Race both policies on identical days/params. Returns per-policy
    summaries + equity curves, ready for the dashboard."""
    out: dict = {"params": params.to_dict(), "policies": {}}
    for policy in POLICIES:
        result = run_trade_backtest(
            signal_days, bars_provider, params,
            policy=policy, starting_equity=starting_equity,
            risk_free_rate=risk_free_rate,
        )
        summary = result.summary_dict()
        summary["equity_curve"] = [round(v, 2) for v in result.equity_curve]
        by_type: dict[str, dict] = {}
        for t in result.trades:
            if t.trade_type == "no_trade":
                continue
            bucket = by_type.setdefault(t.trade_type, {"trades": 0, "wins": 0, "pnl": 0.0})
            bucket["trades"] += 1
            bucket["wins"] += int(t.pnl > 0)
            bucket["pnl"] = round(bucket["pnl"] + t.pnl, 2)
        summary["by_trade_type"] = by_type
        out["policies"][policy] = summary
    return out

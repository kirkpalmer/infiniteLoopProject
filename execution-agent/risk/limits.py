"""
risk/limits.py — Hard-coded safety limits for the execution agent.

These values are CONSTANTS, not configuration. They cannot be changed by
Hermes, by sweep parameters, or by environment variables. They exist to
prevent a bug or a bad strategy from blowing up the account.

If you are ever tempted to soften these limits, re-read CLAUDE.md §Risk Rules.
"""

from __future__ import annotations

import logging

from constants import (
    MAX_CONTRACTS,
    MAX_DAILY_LOSS_PCT,
    MAX_RISK_PER_TRADE_PCT,
    SINGLE_CONTRACT_CAP,
    SPX_MULTIPLIER,
)

LOGGER = logging.getLogger("infiniteloop.risk.limits")


# ── Per-trade sizing ───────────────────────────────────────────────────────────

def contracts_allowed(equity: float, max_loss_per_contract: float) -> int:
    """
    Return the number of contracts allowed under the 10% per-trade risk rule.

    Rules (in priority order):
      1. If max loss of ONE contract exceeds the risk budget → 0 (skip trade).
      2. 1 contract maximum while equity ≤ SINGLE_CONTRACT_CAP ($15,000).
      3. Above $15k: floor(budget / max_loss_per_contract), capped at MAX_CONTRACTS.

    Args:
        equity: Current account equity in dollars.
        max_loss_per_contract: Worst-case loss per contract in dollars
                               = (spread_width - credit) × SPX_MULTIPLIER.
    Returns:
        Number of contracts (0 = skip this trade entirely).
    """
    if max_loss_per_contract <= 0:
        LOGGER.error("max_loss_per_contract must be positive, got %s", max_loss_per_contract)
        return 0

    budget = equity * MAX_RISK_PER_TRADE_PCT

    if max_loss_per_contract > budget:
        LOGGER.info(
            "Trade skipped — max loss $%.2f exceeds risk budget $%.2f (%.0f%% of $%.0f equity)",
            max_loss_per_contract, budget, MAX_RISK_PER_TRADE_PCT * 100, equity,
        )
        return 0

    if equity <= SINGLE_CONTRACT_CAP:
        return 1

    n = int(budget // max_loss_per_contract)
    return min(max(n, 1), MAX_CONTRACTS)


# ── Daily halt ─────────────────────────────────────────────────────────────────

def daily_halt_triggered(starting_equity: float, current_equity: float) -> bool:
    """
    Return True if the daily loss limit has been hit.

    The limit is -5% of the equity at the START of the day (not the current
    equity, which would make the limit a moving target).
    """
    if starting_equity <= 0:
        return False
    daily_loss_pct = (current_equity - starting_equity) / starting_equity
    triggered = daily_loss_pct <= -MAX_DAILY_LOSS_PCT
    if triggered:
        LOGGER.critical(
            "DAILY HALT TRIGGERED — daily P&L %.2f%% (limit %.2f%%). "
            "No more trading today.",
            daily_loss_pct * 100, -MAX_DAILY_LOSS_PCT * 100,
        )
    return triggered


# ── Spread sanity ──────────────────────────────────────────────────────────────

def validate_spread(
    spread_type: str,
    short_strike: float,
    long_strike: float,
    credit: float,
    spread_width: float,
) -> tuple[bool, str]:
    """
    Basic sanity checks before sending an order to Webull.
    Returns (ok, reason).
    """
    if spread_type == "bull_put":
        if short_strike <= long_strike:
            return False, f"bull_put: short {short_strike} must be > long {long_strike}"
    elif spread_type == "bear_call":
        if short_strike >= long_strike:
            return False, f"bear_call: short {short_strike} must be < long {long_strike}"
    elif spread_type == "iron_condor":
        pass  # selector validates both wings separately
    else:
        return False, f"Unknown spread_type '{spread_type}'"

    if credit <= 0:
        return False, f"Credit must be positive, got {credit}"

    if credit >= spread_width:
        return False, f"Credit {credit} >= spread_width {spread_width} — impossible pricing"

    return True, ""

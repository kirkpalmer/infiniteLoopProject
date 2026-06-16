"""
risk/manager.py — Position sizing and per-trade risk calculation.

Translates spread parameters into concrete contract counts and dollar
risk figures. The hard limits live in limits.py; this module applies them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from constants import DEFAULT_SPREAD_WIDTH, SPX_MULTIPLIER
from risk.limits import contracts_allowed, validate_spread

LOGGER = logging.getLogger("infiniteloop.risk.manager")


@dataclass
class TradeSize:
    """Output of size_trade() — everything the order manager needs."""
    spread_type: str          # bull_put | bear_call | iron_condor
    short_strike: float
    long_strike: float
    credit_per_contract: float   # points
    spread_width: float          # points
    max_loss_per_contract: float # dollars = (width - credit) × 100
    contracts: int               # 0 = skip this trade
    total_max_loss: float        # dollars = max_loss_per_contract × contracts
    skip_reason: str             # non-empty when contracts == 0


def size_trade(
    spread_type: str,
    short_strike: float,
    long_strike: float,
    credit: float,              # points collected
    equity: float,
    spread_width: float = DEFAULT_SPREAD_WIDTH,
) -> TradeSize:
    """
    Apply risk rules to determine contract count.

    Returns a TradeSize with contracts=0 and a skip_reason if the trade
    should not be placed.
    """
    def skip(reason: str) -> TradeSize:
        return TradeSize(
            spread_type=spread_type,
            short_strike=short_strike,
            long_strike=long_strike,
            credit_per_contract=credit,
            spread_width=spread_width,
            max_loss_per_contract=0.0,
            contracts=0,
            total_max_loss=0.0,
            skip_reason=reason,
        )

    # Sanity check the spread structure
    ok, reason = validate_spread(spread_type, short_strike, long_strike, credit, spread_width)
    if not ok:
        LOGGER.warning("Spread validation failed: %s", reason)
        return skip(f"invalid_spread: {reason}")

    max_loss_points = spread_width - credit
    if max_loss_points <= 0:
        return skip("max_loss_nonpositive")

    max_loss_dollars = max_loss_points * SPX_MULTIPLIER
    n = contracts_allowed(equity, max_loss_dollars)

    if n == 0:
        return skip("risk_budget_exceeded")

    return TradeSize(
        spread_type=spread_type,
        short_strike=short_strike,
        long_strike=long_strike,
        credit_per_contract=credit,
        spread_width=spread_width,
        max_loss_per_contract=max_loss_dollars,
        contracts=n,
        total_max_loss=max_loss_dollars * n,
        skip_reason="",
    )

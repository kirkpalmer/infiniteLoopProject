"""
spreads/selector.py — Select the spread type and strikes based on Oracle direction.

Given a ClassificationResult (UP/DOWN/NEUTRAL) and the current SPX price,
this module determines:
  - Which spread to trade (bull_put / bear_call / iron_condor / skip)
  - The specific strike prices (short and long legs)

Strike selection uses the expected-move formula with a tunable delta multiplier:
  expected_move = spx_price × (vix / 100) × sqrt(1/252)
  short_strike  = round_to_increment(spx_price - em × em_mult_high) [bull_put]
                = round_to_increment(spx_price + em × em_mult_high) [bear_call]

The em_mult_high and spread_width come from the active strategy's TradeParams.

Iron condors use em_mult_condor for both wings:
  put_short  = spx_price - em × condor_em_mult
  put_long   = put_short  - spread_width
  call_short = spx_price + em × condor_em_mult
  call_long  = call_short + spread_width
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from constants import DEFAULT_SPREAD_WIDTH, STRIKE_INCREMENT

LOGGER = logging.getLogger("infiniteloop.spreads.selector")


@dataclass
class SpreadLegs:
    """
    One vertical spread (or one wing of an iron condor).
    All prices in SPX points.
    """
    spread_type: str        # bull_put | bear_call | iron_condor_put_wing | iron_condor_call_wing
    short_strike: float
    long_strike: float
    spread_width: float
    direction: str          # UP | DOWN | NEUTRAL


@dataclass
class SelectedSpread:
    """
    The spread(s) to trade today.
    For iron condors, both put_wing and call_wing are populated.
    For directional spreads, only primary is set.
    """
    direction: str              # UP | DOWN | NEUTRAL | SKIP
    skip_reason: str            # non-empty when direction == SKIP
    primary: Optional[SpreadLegs] = None    # bull_put (UP) or bear_call (DOWN)
    put_wing: Optional[SpreadLegs] = None   # iron condor put wing (NEUTRAL)
    call_wing: Optional[SpreadLegs] = None  # iron condor call wing (NEUTRAL)
    expected_move: float = 0.0
    spx_price: float = 0.0
    vix: float = 0.0

    def all_legs(self) -> list[SpreadLegs]:
        legs = []
        if self.primary:
            legs.append(self.primary)
        if self.put_wing:
            legs.append(self.put_wing)
        if self.call_wing:
            legs.append(self.call_wing)
        return legs

    def to_dict(self) -> dict:
        d: dict = {
            "direction": self.direction,
            "skip_reason": self.skip_reason,
            "expected_move": round(self.expected_move, 2),
            "spx_price": round(self.spx_price, 2),
            "vix": round(self.vix, 2),
        }
        if self.primary:
            d["primary"] = {
                "spread_type": self.primary.spread_type,
                "short_strike": self.primary.short_strike,
                "long_strike": self.primary.long_strike,
                "spread_width": self.primary.spread_width,
            }
        if self.put_wing:
            d["put_wing"] = {
                "spread_type": self.put_wing.spread_type,
                "short_strike": self.put_wing.short_strike,
                "long_strike": self.put_wing.long_strike,
            }
        if self.call_wing:
            d["call_wing"] = {
                "spread_type": self.call_wing.spread_type,
                "short_strike": self.call_wing.short_strike,
                "long_strike": self.call_wing.long_strike,
            }
        return d


def _round_strike(price: float, increment: float = STRIKE_INCREMENT) -> float:
    """Round to the nearest valid SPX strike increment."""
    return round(round(price / increment) * increment, 2)


def _expected_move(spx_price: float, vix: float) -> float:
    """
    Expected move formula: SPX × (VIX/100) × sqrt(1/252).
    This is the ATM straddle approximation — the 1-sigma daily range.
    """
    if spx_price <= 0 or vix <= 0:
        return 0.0
    return spx_price * (vix / 100.0) * math.sqrt(1.0 / 252.0)


def select_spread(
    direction: str,
    spx_price: float,
    vix: float,
    em_mult_high: float = 0.5,
    em_mult_low: float = 1.2,
    spread_width: float = DEFAULT_SPREAD_WIDTH,
    condor_em_mult: float = 1.2,
    skip_reason: str = "",
) -> SelectedSpread:
    """
    Select the spread structure and strikes.

    Args:
        direction:       Oracle output — UP / DOWN / NEUTRAL / SKIP
        spx_price:       Current SPX (or ES-equivalent) price
        vix:             Current VIX for expected-move calculation
        em_mult_high:    Strike offset multiplier for directional spreads
                         (how far OTM to sell — smaller = closer to ATM)
        em_mult_low:     (unused directly — kept for symmetry with TradeParams)
        spread_width:    Width between short and long strikes in SPX points
        condor_em_mult:  Strike offset multiplier for iron condor wings
        skip_reason:     Pass-through from classifier (pre-populated for SKIP)

    Returns:
        SelectedSpread — legs are None if direction == SKIP
    """
    if direction == "SKIP" or spx_price <= 0:
        reason = skip_reason or ("zero_price" if spx_price <= 0 else "classifier_skip")
        LOGGER.info("Spread selection skipped: %s", reason)
        return SelectedSpread(direction="SKIP", skip_reason=reason)

    em = _expected_move(spx_price, vix)
    if em <= 0:
        LOGGER.warning("Expected move is zero — cannot select strikes (price=%.2f vix=%.1f)", spx_price, vix)
        return SelectedSpread(direction="SKIP", skip_reason="em_zero",
                              expected_move=em, spx_price=spx_price, vix=vix)

    LOGGER.debug(
        "Expected move: %.2f pts (SPX=%.2f VIX=%.1f)",
        em, spx_price, vix,
    )

    if direction == "UP":
        # Bull put spread — sell put below market, buy further below
        short_strike = _round_strike(spx_price - em * em_mult_high)
        long_strike  = _round_strike(short_strike - spread_width)
        primary = SpreadLegs(
            spread_type="bull_put",
            short_strike=short_strike,
            long_strike=long_strike,
            spread_width=spread_width,
            direction="UP",
        )
        LOGGER.info(
            "Bull put spread: sell %s / buy %s (SPX %.2f, EM %.2f, em_mult=%.2f)",
            short_strike, long_strike, spx_price, em, em_mult_high,
        )
        return SelectedSpread(
            direction="UP", skip_reason="", primary=primary,
            expected_move=em, spx_price=spx_price, vix=vix,
        )

    if direction == "DOWN":
        # Bear call spread — sell call above market, buy further above
        short_strike = _round_strike(spx_price + em * em_mult_high)
        long_strike  = _round_strike(short_strike + spread_width)
        primary = SpreadLegs(
            spread_type="bear_call",
            short_strike=short_strike,
            long_strike=long_strike,
            spread_width=spread_width,
            direction="DOWN",
        )
        LOGGER.info(
            "Bear call spread: sell %s / buy %s (SPX %.2f, EM %.2f, em_mult=%.2f)",
            short_strike, long_strike, spx_price, em, em_mult_high,
        )
        return SelectedSpread(
            direction="DOWN", skip_reason="", primary=primary,
            expected_move=em, spx_price=spx_price, vix=vix,
        )

    if direction == "NEUTRAL":
        # Iron condor — sell both sides
        put_short  = _round_strike(spx_price - em * condor_em_mult)
        put_long   = _round_strike(put_short - spread_width)
        call_short = _round_strike(spx_price + em * condor_em_mult)
        call_long  = _round_strike(call_short + spread_width)

        put_wing = SpreadLegs(
            spread_type="iron_condor_put_wing",
            short_strike=put_short,
            long_strike=put_long,
            spread_width=spread_width,
            direction="NEUTRAL",
        )
        call_wing = SpreadLegs(
            spread_type="iron_condor_call_wing",
            short_strike=call_short,
            long_strike=call_long,
            spread_width=spread_width,
            direction="NEUTRAL",
        )
        LOGGER.info(
            "Iron condor: put wing %s/%s | call wing %s/%s (SPX %.2f, EM %.2f)",
            put_short, put_long, call_short, call_long, spx_price, em,
        )
        return SelectedSpread(
            direction="NEUTRAL", skip_reason="",
            put_wing=put_wing, call_wing=call_wing,
            expected_move=em, spx_price=spx_price, vix=vix,
        )

    LOGGER.warning("Unknown direction '%s' — skipping", direction)
    return SelectedSpread(direction="SKIP", skip_reason=f"unknown_direction:{direction}")

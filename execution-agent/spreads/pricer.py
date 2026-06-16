"""
spreads/pricer.py — Real-time spread pricing via Webull Python SDK.

Given a SpreadLegs object (short/long strikes, spread type), this module:
  1. Fetches live bid/ask quotes for both legs via DataClient.option_market_data
  2. Computes the net credit for the spread (short bid - long ask)
  3. Returns a SpreadQuote with mid-price, bid/ask bounds, and validity flag

OCC option symbol format used by the Webull SDK:
  SPXW{YYMMDD}{P|C}{strike*1000:08d}
  e.g. SPXW250615P05750000  →  SPXW put expiring 2025-06-15, strike $5750

Spread credit calculation:
  - Bull put:   sell put short_strike (take bid), buy put long_strike (pay ask)
                net_credit = short_bid - long_ask
  - Bear call:  sell call short_strike (take bid), buy call long_strike (pay ask)
                net_credit = short_bid - long_ask
  - Iron condor: sum both wings

All prices are in SPX points ($100 = 1 point, SPX_MULTIPLIER).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytz

from constants import TICK_SIZE
from spreads.selector import SpreadLegs
import config

LOGGER = logging.getLogger("infiniteloop.spreads.pricer")

EASTERN = pytz.timezone("US/Eastern")

# Age limit for a quote to be considered "live" (seconds)
MAX_QUOTE_AGE_SECONDS = 30


def _occ_symbol(strike: float, option_type: str, expiry: str) -> str:
    """
    Build an OCC-format option symbol for the Webull SDK.

    Args:
        strike:      strike price (e.g. 5750.0)
        option_type: "P" or "C"
        expiry:      YYMMDD string (e.g. "250615")

    Returns:
        e.g. "SPXW250615P05750000"
    """
    strike_int = int(round(strike * 1000))
    return f"SPXW{expiry}{option_type}{strike_int:08d}"


@dataclass
class LegQuote:
    """Bid/ask for a single option strike."""
    strike: float
    option_type: str    # P or C
    bid: float
    ask: float
    mid: float
    last: float
    volume: int
    open_interest: int
    quoted_at: datetime

    @classmethod
    def from_sdk(cls, strike: float, opt_type: str, data) -> "LegQuote":
        """Build from SDK SnapshotResult or plain dict."""
        if hasattr(data, "bid_price"):
            bid  = float(data.bid_price or 0)
            ask  = float(data.ask_price or 0)
            last = float(getattr(data, "close", None) or getattr(data, "last_price", None) or 0)
            vol  = int(getattr(data, "volume", None) or 0)
            oi   = int(getattr(data, "open_interest", None) or 0)
        else:
            d    = data if isinstance(data, dict) else {}
            bid  = float(d.get("bidPrice") or d.get("bid_price") or 0)
            ask  = float(d.get("askPrice") or d.get("ask_price") or 0)
            last = float(d.get("close") or d.get("lastPrice") or d.get("last_price") or 0)
            vol  = int(d.get("volume") or 0)
            oi   = int(d.get("openInterest") or d.get("open_interest") or 0)

        mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else last
        return cls(
            strike=strike, option_type=opt_type,
            bid=bid, ask=ask, mid=mid, last=last,
            volume=vol, open_interest=oi,
            quoted_at=datetime.now(EASTERN),
        )

    @property
    def is_tradeable(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class SpreadQuote:
    """Full spread pricing result."""
    spread_type: str
    short_leg: LegQuote
    long_leg: LegQuote
    net_credit: float           # short_bid - long_ask (> 0 when valid)
    max_loss_points: float      # spread_width - net_credit
    max_profit_points: float    # = net_credit
    spread_width: float
    is_valid: bool
    invalid_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "spread_type": self.spread_type,
            "short_strike": self.short_leg.strike,
            "long_strike": self.long_leg.strike,
            "short_bid": self.short_leg.bid,
            "long_ask": self.long_leg.ask,
            "net_credit": round(self.net_credit, 2),
            "max_loss_points": round(self.max_loss_points, 2),
            "spread_width": self.spread_width,
            "is_valid": self.is_valid,
            "invalid_reason": self.invalid_reason,
        }


@dataclass
class IronCondorQuote:
    """Combined iron condor pricing — both wings."""
    put_wing: SpreadQuote
    call_wing: SpreadQuote
    net_credit: float           # total credit from both wings
    max_loss_points: float      # max(spread_widths) - net_credit
    spread_width: float
    is_valid: bool
    invalid_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "put_wing": self.put_wing.to_dict(),
            "call_wing": self.call_wing.to_dict(),
            "net_credit": round(self.net_credit, 2),
            "max_loss_points": round(self.max_loss_points, 2),
            "spread_width": self.spread_width,
            "is_valid": self.is_valid,
            "invalid_reason": self.invalid_reason,
        }


class SpreadPricer:
    """
    Options pricer using the Webull SDK DataClient.

    All methods are async (run in the event loop). The underlying SDK calls
    are synchronous and run in a thread executor so they don't block the loop.

    Usage:
        pricer = SpreadPricer(api_client=config.build_api_client())
        quote = await pricer.price_spread(legs)
    """

    def __init__(self, api_client=None) -> None:
        self._api_client = api_client or config.build_api_client()
        self._data_client = None   # lazy-init after SDK import

    def _get_data_client(self):
        if self._data_client is None:
            from webull.data.data_client import DataClient
            self._data_client = DataClient(self._api_client)
        return self._data_client

    async def close(self) -> None:
        pass  # SDK clients don't need explicit teardown

    # ── Public methods ────────────────────────────────────────────────────────

    async def price_spread(self, legs: SpreadLegs) -> SpreadQuote:
        """
        Price a single vertical spread by fetching live quotes for both legs.
        Returns SpreadQuote with is_valid=False on any error.
        """
        st = legs.spread_type
        opt_type = "P" if "put" in st else "C"
        expiry_occ = self._today_expiry_occ()   # YYMMDD

        short_sym = _occ_symbol(legs.short_strike, opt_type, expiry_occ)
        long_sym  = _occ_symbol(legs.long_strike,  opt_type, expiry_occ)

        try:
            short_data, long_data = await asyncio.gather(
                self._fetch_quote(short_sym),
                self._fetch_quote(long_sym),
            )
        except Exception as exc:
            LOGGER.error("Quote fetch failed for %s: %s", st, exc)
            return self._invalid_spread(legs, f"api_error: {exc}")

        short_leg = LegQuote.from_sdk(legs.short_strike, opt_type, short_data)
        long_leg  = LegQuote.from_sdk(legs.long_strike,  opt_type, long_data)

        if not short_leg.is_tradeable:
            return self._invalid_spread(
                legs, f"short_leg_no_market: bid={short_leg.bid} ask={short_leg.ask}",
                short_leg, long_leg,
            )
        if not long_leg.is_tradeable:
            return self._invalid_spread(
                legs, f"long_leg_no_market: bid={long_leg.bid} ask={long_leg.ask}",
                short_leg, long_leg,
            )

        net_credit = round(short_leg.bid - long_leg.ask, 2)
        if net_credit <= 0:
            return self._invalid_spread(
                legs,
                f"negative_credit: short_bid={short_leg.bid} long_ask={long_leg.ask}",
                short_leg, long_leg,
            )

        max_loss = round(legs.spread_width - net_credit, 2)
        LOGGER.info(
            "Priced %s: credit=%.2f max_loss=%.2f (short %s bid=%.2f, long %s ask=%.2f)",
            st, net_credit, max_loss,
            legs.short_strike, short_leg.bid,
            legs.long_strike, long_leg.ask,
        )
        return SpreadQuote(
            spread_type=st,
            short_leg=short_leg, long_leg=long_leg,
            net_credit=net_credit, max_loss_points=max_loss,
            max_profit_points=net_credit,
            spread_width=legs.spread_width,
            is_valid=True,
        )

    async def price_iron_condor(
        self, put_wing: SpreadLegs, call_wing: SpreadLegs,
    ) -> IronCondorQuote:
        """Price both wings concurrently."""
        put_q, call_q = await asyncio.gather(
            self.price_spread(put_wing),
            self.price_spread(call_wing),
        )

        if not put_q.is_valid:
            return IronCondorQuote(
                put_wing=put_q, call_wing=call_q,
                net_credit=0.0, max_loss_points=put_wing.spread_width,
                spread_width=put_wing.spread_width, is_valid=False,
                invalid_reason=f"put_wing_invalid: {put_q.invalid_reason}",
            )
        if not call_q.is_valid:
            return IronCondorQuote(
                put_wing=put_q, call_wing=call_q,
                net_credit=0.0, max_loss_points=call_wing.spread_width,
                spread_width=call_wing.spread_width, is_valid=False,
                invalid_reason=f"call_wing_invalid: {call_q.invalid_reason}",
            )

        net_credit  = round(put_q.net_credit + call_q.net_credit, 2)
        spread_width = max(put_wing.spread_width, call_wing.spread_width)
        max_loss    = round(spread_width - net_credit, 2)

        LOGGER.info("Iron condor priced: total_credit=%.2f max_loss=%.2f", net_credit, max_loss)
        return IronCondorQuote(
            put_wing=put_q, call_wing=call_q,
            net_credit=net_credit, max_loss_points=max_loss,
            spread_width=spread_width, is_valid=True,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _fetch_quote(self, occ_symbol: str):
        """
        Fetch a single option snapshot via SDK DataClient.
        Runs the synchronous SDK call in the thread executor so it doesn't
        block the event loop.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_quote_sync, occ_symbol)

    def _fetch_quote_sync(self, occ_symbol: str):
        """Synchronous SDK call — executed in thread executor."""
        from webull.api.enum import Category

        data_client = self._get_data_client()
        LOGGER.debug("Fetching option quote: %s", occ_symbol)

        resp = data_client.option_market_data.get_option_snapshot(
            symbols=[occ_symbol],
            category=Category.US_OPTION,
        )

        # SDK wraps response; extract first item
        if hasattr(resp, "body") and resp.body:
            items = resp.body if isinstance(resp.body, list) else [resp.body]
            if items:
                return items[0]
        # Fallback: try dict-style body
        if isinstance(resp, dict):
            data = resp.get("data") or resp
            items = data if isinstance(data, list) else [data]
            if items:
                return items[0]

        LOGGER.warning("Empty or unexpected snapshot response for %s: %r", occ_symbol, resp)
        return {}

    @staticmethod
    def _today_expiry_occ() -> str:
        """Return today's date in YYMMDD format for OCC symbols."""
        return datetime.now(EASTERN).strftime("%y%m%d")

    @staticmethod
    def _invalid_spread(
        legs: SpreadLegs,
        reason: str,
        short_leg: Optional[LegQuote] = None,
        long_leg:  Optional[LegQuote] = None,
    ) -> SpreadQuote:
        opt_type = "P" if "put" in legs.spread_type else "C"
        _empty = LegQuote(
            strike=0, option_type=opt_type,
            bid=0, ask=0, mid=0, last=0, volume=0, open_interest=0,
            quoted_at=datetime.now(EASTERN),
        )
        return SpreadQuote(
            spread_type=legs.spread_type,
            short_leg=short_leg or _empty,
            long_leg=long_leg  or _empty,
            net_credit=0.0,
            max_loss_points=legs.spread_width,
            max_profit_points=0.0,
            spread_width=legs.spread_width,
            is_valid=False,
            invalid_reason=reason,
        )

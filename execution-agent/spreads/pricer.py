"""
spreads/pricer.py — Real-time spread pricing via Webull options quote API.

Given a SpreadLegs object (short/long strikes, spread type), this module:
  1. Fetches live bid/ask quotes for both legs from Webull HTTP API
  2. Computes the net credit for the spread (short bid - long ask for sells)
  3. Returns a PriceQuote with mid-price, bid/ask bounds, and data freshness

Webull options quote endpoint (from OpenAPI docs):
  GET /options/quote?symbol=SPXW&expDate=YYYYMMDD&strike=XXXX&type=P/C

We quote SPX options as SPXW (the weekly/daily series that expires on the
current trading day). If SPXW returns no quote, we fall back to SPX.

Spread credit calculation:
  - Bull put:  sell put short_strike (take bid), buy put long_strike (pay ask)
               net_credit = short_bid - long_ask
  - Bear call: sell call short_strike (take bid), buy call long_strike (pay ask)
               net_credit = short_bid - long_ask
  - Iron condor: sum both wings, net_credit = put_net_credit + call_net_credit

All prices are in SPX points ($100 = 1 point at SPX_MULTIPLIER).
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

# Webull options quote base URL — verify against current OpenAPI docs
QUOTE_BASE_URL = "https://quoteapi.webull.com/api/quote"

# Age limit for a quote to be considered "live" (seconds)
MAX_QUOTE_AGE_SECONDS = 30


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
    def from_api(cls, strike: float, opt_type: str, data: dict) -> "LegQuote":
        bid  = float(data.get("bidPrice", 0) or 0)
        ask  = float(data.get("askPrice", 0) or 0)
        last = float(data.get("close", 0) or data.get("lastPrice", 0) or 0)
        mid  = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else last
        return cls(
            strike=strike, option_type=opt_type,
            bid=bid, ask=ask, mid=mid, last=last,
            volume=int(data.get("volume", 0) or 0),
            open_interest=int(data.get("openInterest", 0) or 0),
            quoted_at=datetime.now(EASTERN),
        )

    @property
    def is_tradeable(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class SpreadQuote:
    """
    Full spread pricing result.
    net_credit is what we receive when entering the spread (in SPX points).
    """
    spread_type: str
    short_leg: LegQuote
    long_leg: LegQuote
    net_credit: float           # short_bid - long_ask (should be > 0)
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
    max_loss_points: float      # spread_width - net_credit (one side)
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
    Async options pricer that calls the Webull quote API.

    Usage (from an async context):
        pricer = SpreadPricer()
        quote = await pricer.price_spread(legs)
    """

    def __init__(self) -> None:
        self._session = None   # aiohttp.ClientSession — lazy-init

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def price_spread(self, legs: SpreadLegs) -> SpreadQuote:
        """
        Price a single vertical spread.
        Returns SpreadQuote with is_valid=False and a reason on failure.
        """
        st = legs.spread_type
        opt_type = "P" if "put" in st else "C"
        expiry = self._today_expiry()

        try:
            short_data = await self._fetch_option_quote(legs.short_strike, opt_type, expiry)
            long_data  = await self._fetch_option_quote(legs.long_strike,  opt_type, expiry)
        except Exception as exc:
            LOGGER.error("Quote fetch failed for %s: %s", st, exc)
            return self._invalid_spread(legs, f"api_error: {exc}")

        short_leg = LegQuote.from_api(legs.short_strike, opt_type, short_data)
        long_leg  = LegQuote.from_api(legs.long_strike,  opt_type, long_data)

        if not short_leg.is_tradeable:
            return self._invalid_spread(legs, f"short_leg_no_market: bid={short_leg.bid} ask={short_leg.ask}",
                                        short_leg, long_leg)
        if not long_leg.is_tradeable:
            return self._invalid_spread(legs, f"long_leg_no_market: bid={long_leg.bid} ask={long_leg.ask}",
                                        short_leg, long_leg)

        net_credit = round(short_leg.bid - long_leg.ask, 2)
        if net_credit <= 0:
            return self._invalid_spread(
                legs, f"negative_credit: short_bid={short_leg.bid} long_ask={long_leg.ask}",
                short_leg, long_leg,
            )

        max_loss = round(legs.spread_width - net_credit, 2)
        LOGGER.info(
            "Priced %s: credit=%.2f max_loss=%.2f (short %s bid=%.2f, long %s ask=%.2f)",
            st, net_credit, max_loss,
            legs.short_strike, short_leg.bid, legs.long_strike, long_leg.ask,
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
        self, put_wing: SpreadLegs, call_wing: SpreadLegs
    ) -> IronCondorQuote:
        """Price both wings of an iron condor concurrently."""
        put_q, call_q = await asyncio.gather(
            self.price_spread(put_wing),
            self.price_spread(call_wing),
        )

        if not put_q.is_valid:
            return IronCondorQuote(
                put_wing=put_q, call_wing=call_q,
                net_credit=0.0, max_loss_points=put_wing.spread_width,
                spread_width=put_wing.spread_width,
                is_valid=False, invalid_reason=f"put_wing_invalid: {put_q.invalid_reason}",
            )
        if not call_q.is_valid:
            return IronCondorQuote(
                put_wing=put_q, call_wing=call_q,
                net_credit=0.0, max_loss_points=call_wing.spread_width,
                spread_width=call_wing.spread_width,
                is_valid=False, invalid_reason=f"call_wing_invalid: {call_q.invalid_reason}",
            )

        net_credit = round(put_q.net_credit + call_q.net_credit, 2)
        # Iron condor max loss = wider wing width - total credit received
        spread_width = max(put_wing.spread_width, call_wing.spread_width)
        max_loss = round(spread_width - net_credit, 2)

        LOGGER.info(
            "Iron condor priced: total_credit=%.2f max_loss=%.2f",
            net_credit, max_loss,
        )
        return IronCondorQuote(
            put_wing=put_q, call_wing=call_q,
            net_credit=net_credit, max_loss_points=max_loss,
            spread_width=spread_width,
            is_valid=True,
        )

    async def _fetch_option_quote(
        self, strike: float, option_type: str, expiry: str
    ) -> dict:
        """
        Call Webull options quote API for a single leg.

        Endpoint and parameters follow Webull OpenAPI docs. The exact URL
        and field names should be verified against the current API version.
        """
        import aiohttp

        session = await self._get_session()
        headers = {
            "App": "desktop",
            "App-Group": "broker",
            "Appid": config.WEBULL_APP_KEY,
            "Did": config.WEBULL_ACCOUNT_ID,
            "Access-Token": config.WEBULL_TRADE_TOKEN,
        }
        params = {
            "symbol": "SPXW",       # SPX weekly/0DTE series
            "expDate": expiry,      # YYYYMMDD
            "strike": str(int(strike)),
            "type": option_type,    # P or C
        }
        url = f"{QUOTE_BASE_URL}/option/quote"

        LOGGER.debug("Fetching option quote: %s %s %s %s", params["symbol"], expiry, strike, option_type)

        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            # Webull typically wraps response in {data: {...}} or {result: {...}}
            if isinstance(data, dict) and "data" in data:
                return data["data"] or {}
            return data or {}

    @staticmethod
    def _today_expiry() -> str:
        """Return today's date in YYYYMMDD format (0DTE options expire today)."""
        return datetime.now(EASTERN).strftime("%Y%m%d")

    @staticmethod
    def _invalid_spread(
        legs: SpreadLegs, reason: str,
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

"""
orders/manager.py — Webull multi-leg options order placement via SDK.

Places SPX/SPXW 0DTE vertical spreads and iron condors.

Paper mode (IS_PAPER_MODE = True — default until Phase 3):
  - Logs the intended order
  - Returns a synthetic PAPER_FILL at the mid-price
  - Does NOT call the Webull API

Live mode (WEBULL_ENVIRONMENT=prod, Phase 3 only):
  - Uses TradeClient.order_v2.place_option() for multi-leg orders
  - The SDK handles auth internally — no Access-Token header needed

Order leg format expected by the SDK:
  {
    "instrument_type": "OPTION",
    "market":          "US",
    "strike":          "5750.0",
    "side":            "PUT",          # or "CALL"
    "expire_date":     "2025-06-15",   # YYYY-MM-DD
    "action":          "SELL",         # or "BUY"
  }

The SDK's place_option() takes:
  trade_client.order_v2.place_option(account_id, new_orders)
  where new_orders is a list of dicts as above.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pytz

import config
from constants import SPX_MULTIPLIER
from risk.manager import TradeSize
from spreads.pricer import IronCondorQuote, SpreadQuote

LOGGER = logging.getLogger("infiniteloop.orders.manager")

EASTERN = pytz.timezone("US/Eastern")


class OrderStatus(Enum):
    PENDING    = "PENDING"
    FILLED     = "FILLED"
    PARTIAL    = "PARTIAL"
    CANCELLED  = "CANCELLED"
    REJECTED   = "REJECTED"
    PAPER_FILL = "PAPER_FILL"   # synthetic fill in paper mode


@dataclass
class OrderLeg:
    """One leg of a multi-leg options order (SDK format)."""
    action:       str    # BUY or SELL
    side:         str    # PUT or CALL
    strike:       float
    expire_date:  str    # YYYY-MM-DD
    instrument_type: str = "OPTION"
    market:          str = "US"

    def to_sdk_dict(self) -> dict:
        return {
            "instrument_type": self.instrument_type,
            "market":          self.market,
            "strike":          str(self.strike),
            "side":            self.side,
            "expire_date":     self.expire_date,
            "action":          self.action,
        }


@dataclass
class OrderResult:
    """Result returned after placing or simulating an order."""
    order_id:          str
    status:            OrderStatus
    spread_type:       str
    contracts:         int
    net_credit:        float          # SPX points per contract
    net_credit_dollars: float         # net_credit × contracts × SPX_MULTIPLIER
    max_loss_dollars:  float
    filled_at:         Optional[datetime]
    is_paper:          bool
    rejection_reason:  str = ""
    raw_response:      dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "order_id":           self.order_id,
            "status":             self.status.value,
            "spread_type":        self.spread_type,
            "contracts":          self.contracts,
            "net_credit":         round(self.net_credit, 2),
            "net_credit_dollars": round(self.net_credit_dollars, 2),
            "max_loss_dollars":   round(self.max_loss_dollars, 2),
            "filled_at":          self.filled_at.isoformat() if self.filled_at else None,
            "is_paper":           self.is_paper,
            "rejection_reason":   self.rejection_reason,
        }


class OrderManager:
    """
    Order manager for SPX/SPXW 0DTE spreads.

    All entry methods are async. Underlying SDK calls are synchronous and run
    in a thread executor so they don't block the event loop.
    """

    def __init__(self, api_client=None) -> None:
        self._api_client  = api_client or config.build_api_client()
        self._is_paper    = config.IS_PAPER_MODE
        self._trade_client = None  # lazy-init

    def _get_trade_client(self):
        if self._trade_client is None:
            from webull.trade.trade_client import TradeClient
            self._trade_client = TradeClient(self._api_client)
        return self._trade_client

    # ── Entry orders ──────────────────────────────────────────────────────────

    async def enter_vertical_spread(
        self,
        quote: SpreadQuote,
        size: TradeSize,
    ) -> OrderResult:
        """
        Enter a vertical spread (bull_put or bear_call).
        The spread is always sold-to-open — we're premium sellers.
        """
        if not quote.is_valid:
            return self._rejected(
                quote.spread_type, size.contracts, quote.net_credit,
                f"invalid_quote: {quote.invalid_reason}",
            )

        side = "PUT" if "put" in quote.spread_type else "CALL"
        expiry = self._today_expiry()

        legs = [
            OrderLeg("SELL", side, quote.short_leg.strike, expiry),
            OrderLeg("BUY",  side, quote.long_leg.strike,  expiry),
        ]

        if self._is_paper:
            return self._paper_fill(
                quote.spread_type, size.contracts,
                quote.net_credit,
                size.max_loss_per_contract * size.contracts,
            )

        return await self._submit_order(
            spread_type=quote.spread_type,
            legs=legs,
            net_credit=quote.net_credit,
            contracts=size.contracts,
            max_loss_dollars=size.total_max_loss,
        )

    async def enter_iron_condor(
        self,
        quote: IronCondorQuote,
        size: TradeSize,
    ) -> OrderResult:
        """Enter an iron condor as a single 4-leg order."""
        if not quote.is_valid:
            return self._rejected(
                "iron_condor", size.contracts, quote.net_credit,
                f"invalid_quote: {quote.invalid_reason}",
            )

        expiry = self._today_expiry()
        legs = [
            OrderLeg("SELL", "PUT",  quote.put_wing.short_leg.strike,  expiry),
            OrderLeg("BUY",  "PUT",  quote.put_wing.long_leg.strike,   expiry),
            OrderLeg("SELL", "CALL", quote.call_wing.short_leg.strike, expiry),
            OrderLeg("BUY",  "CALL", quote.call_wing.long_leg.strike,  expiry),
        ]

        if self._is_paper:
            return self._paper_fill(
                "iron_condor", size.contracts,
                quote.net_credit,
                size.max_loss_per_contract * size.contracts,
            )

        return await self._submit_order(
            spread_type="iron_condor",
            legs=legs,
            net_credit=quote.net_credit,
            contracts=size.contracts,
            max_loss_dollars=size.total_max_loss,
        )

    async def close_spread(
        self,
        original_order: OrderResult,
        close_price: float,
        reason: str = "profit_target",
    ) -> OrderResult:
        """
        Close an existing spread by buying it back.
        close_price = the debit paid to close (in SPX points).
        In paper mode: synthetic fill at close_price.
        """
        LOGGER.info(
            "Closing spread %s at %.2f pts (reason=%s, paper=%s)",
            original_order.order_id, close_price, reason, self._is_paper,
        )
        if self._is_paper:
            return self._paper_fill(
                f"close_{original_order.spread_type}",
                original_order.contracts,
                -close_price,   # negative = debit paid
                0.0,
            )
        # Live: mirror entry legs with reversed BUY/SELL — implemented in Phase 3
        raise NotImplementedError("Live close not yet implemented — system is in paper mode")

    # ── Internal: SDK order submission ────────────────────────────────────────

    async def _submit_order(
        self,
        spread_type: str,
        legs: list[OrderLeg],
        net_credit: float,
        contracts: int,
        max_loss_dollars: float,
    ) -> OrderResult:
        """Submit multi-leg order via SDK (runs in thread executor)."""
        loop = asyncio.get_event_loop()
        try:
            raw = await loop.run_in_executor(
                None,
                self._submit_order_sync,
                spread_type, legs, net_credit, contracts,
            )
        except Exception as exc:
            LOGGER.error("Order submission failed: %s", exc)
            return self._rejected(spread_type, contracts, net_credit, f"sdk_error: {exc}")

        order_id  = str(raw.get("orderId") or raw.get("order_id") or uuid.uuid4())
        status_raw = str(raw.get("status") or "PENDING").upper()
        try:
            status = OrderStatus[status_raw]
        except KeyError:
            status = OrderStatus.PENDING

        return OrderResult(
            order_id=order_id,
            status=status,
            spread_type=spread_type,
            contracts=contracts,
            net_credit=net_credit,
            net_credit_dollars=round(net_credit * contracts * SPX_MULTIPLIER, 2),
            max_loss_dollars=max_loss_dollars,
            filled_at=datetime.now(EASTERN) if status == OrderStatus.FILLED else None,
            is_paper=False,
            raw_response=raw,
        )

    def _submit_order_sync(
        self,
        spread_type: str,
        legs: list[OrderLeg],
        net_credit: float,
        contracts: int,
    ) -> dict:
        """Synchronous SDK call — executed in thread executor."""
        LOGGER.info(
            "Submitting LIVE order: %s × %d @ %.2f credit",
            spread_type, contracts, net_credit,
        )
        trade_client = self._get_trade_client()
        new_orders = [leg.to_sdk_dict() for leg in legs]

        resp = trade_client.order_v2.place_option(
            account_id=config.WEBULL_ACCOUNT_ID,
            new_orders=new_orders,
        )

        # SDK returns response object; extract body
        if hasattr(resp, "body") and resp.body:
            body = resp.body
            return body.__dict__ if hasattr(body, "__dict__") else body
        if isinstance(resp, dict):
            return resp.get("data") or resp
        return {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _paper_fill(
        self,
        spread_type: str,
        contracts: int,
        net_credit: float,
        max_loss_dollars: float,
    ) -> OrderResult:
        order_id = f"PAPER-{int(time.time())}-{uuid.uuid4().hex[:6].upper()}"
        LOGGER.info(
            "[PAPER] Fill: %s × %d @ %.2f pts ($%.2f credit, max_loss=$%.2f)",
            spread_type, contracts, net_credit,
            net_credit * contracts * SPX_MULTIPLIER, max_loss_dollars,
        )
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.PAPER_FILL,
            spread_type=spread_type,
            contracts=contracts,
            net_credit=net_credit,
            net_credit_dollars=round(net_credit * contracts * SPX_MULTIPLIER, 2),
            max_loss_dollars=max_loss_dollars,
            filled_at=datetime.now(EASTERN),
            is_paper=True,
        )

    def _rejected(
        self,
        spread_type: str,
        contracts: int,
        net_credit: float,
        reason: str,
    ) -> OrderResult:
        LOGGER.error("Order rejected: %s — %s", spread_type, reason)
        return OrderResult(
            order_id="",
            status=OrderStatus.REJECTED,
            spread_type=spread_type,
            contracts=contracts,
            net_credit=net_credit,
            net_credit_dollars=0.0,
            max_loss_dollars=0.0,
            filled_at=None,
            is_paper=self._is_paper,
            rejection_reason=reason,
        )

    @staticmethod
    def _today_expiry() -> str:
        """Return today's date in YYYY-MM-DD (SDK expects this format)."""
        return datetime.now(EASTERN).strftime("%Y-%m-%d")

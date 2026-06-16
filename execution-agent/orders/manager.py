"""
orders/manager.py — Webull multi-leg options order placement via HTTP API.

Places SPX/SPXW 0DTE vertical spreads and iron condors as combo orders.
In paper mode (WEBULL_TRADING_MODE=paper), the API call is logged but no
real order is sent — we fake a fill at the mid-price for paper tracking.

Webull multi-leg order format (from OpenAPI docs):
  POST /order/combo
  {
    "accountId": "...",
    "orderType": "LMT",
    "timeInForce": "DAY",
    "comboType": "VERTICAL",
    "legs": [
      {"action": "SELL", "ratio": 1, "symbol": "SPXW", "expDate": "...", "strike": "...", "right": "P/C"},
      {"action": "BUY",  "ratio": 1, "symbol": "SPXW", "expDate": "...", "strike": "...", "right": "P/C"}
    ],
    "lmtPrice": "X.XX",   # net credit for sells, negative for debit (we always sell)
    "qty": "1"
  }

For paper mode, no HTTP call is made. Returns a synthetic OrderResult with a
paper-fill at the mid-price.
"""

from __future__ import annotations

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

# Webull order API endpoint
ORDER_BASE_URL = "https://tradeapi.webull.com/api/trade"


class OrderStatus(Enum):
    PENDING     = "PENDING"
    FILLED      = "FILLED"
    PARTIAL     = "PARTIAL"
    CANCELLED   = "CANCELLED"
    REJECTED    = "REJECTED"
    PAPER_FILL  = "PAPER_FILL"   # synthetic fill in paper mode


@dataclass
class OrderLeg:
    """One leg of a multi-leg options order."""
    action: str        # BUY or SELL
    symbol: str        # SPXW
    exp_date: str      # YYYYMMDD
    strike: float
    right: str         # P or C
    ratio: int = 1


@dataclass
class OrderResult:
    """Result returned after placing or simulating an order."""
    order_id: str
    status: OrderStatus
    spread_type: str
    contracts: int
    net_credit: float          # SPX points
    net_credit_dollars: float  # net_credit × contracts × SPX_MULTIPLIER
    max_loss_dollars: float
    filled_at: Optional[datetime]
    is_paper: bool
    rejection_reason: str = ""
    raw_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "status": self.status.value,
            "spread_type": self.spread_type,
            "contracts": self.contracts,
            "net_credit": round(self.net_credit, 2),
            "net_credit_dollars": round(self.net_credit_dollars, 2),
            "max_loss_dollars": round(self.max_loss_dollars, 2),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "is_paper": self.is_paper,
            "rejection_reason": self.rejection_reason,
        }


class OrderManager:
    """
    Async order manager for SPX/SPXW 0DTE spreads.

    Paper mode (config.IS_PAPER_MODE == True):
      - Logs the intended order
      - Returns a synthetic PAPER_FILL at the mid-price
      - Does NOT call the Webull API

    Live mode:
      - Posts a multi-leg combo order to Webull HTTP API
      - Returns the actual fill result
    """

    def __init__(self) -> None:
        self._is_paper = config.IS_PAPER_MODE
        self._session  = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Entry orders ──────────────────────────────────────────────────────────

    async def enter_vertical_spread(
        self,
        quote: SpreadQuote,
        size: TradeSize,
    ) -> OrderResult:
        """
        Enter a vertical spread (bull_put or bear_call).
        The spread is always sold-to-open (we're premium sellers).
        """
        if not quote.is_valid:
            return self._rejected(
                quote.spread_type, size.contracts, quote.net_credit,
                f"invalid_quote: {quote.invalid_reason}",
            )

        opt_type = "P" if "put" in quote.spread_type else "C"
        expiry   = self._today_expiry()

        legs = [
            OrderLeg("SELL", "SPXW", expiry, quote.short_leg.strike, opt_type),
            OrderLeg("BUY",  "SPXW", expiry, quote.long_leg.strike,  opt_type),
        ]

        if self._is_paper:
            return self._paper_fill(
                quote.spread_type, size.contracts,
                quote.net_credit, size.max_loss_per_contract * size.contracts,
            )

        return await self._submit_combo_order(
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
        """
        Enter an iron condor.
        Sends as a single 4-leg combo order to Webull.
        """
        if not quote.is_valid:
            return self._rejected(
                "iron_condor", size.contracts, quote.net_credit,
                f"invalid_quote: {quote.invalid_reason}",
            )

        expiry = self._today_expiry()
        legs = [
            OrderLeg("SELL", "SPXW", expiry, quote.put_wing.short_leg.strike,  "P"),
            OrderLeg("BUY",  "SPXW", expiry, quote.put_wing.long_leg.strike,   "P"),
            OrderLeg("SELL", "SPXW", expiry, quote.call_wing.short_leg.strike, "C"),
            OrderLeg("BUY",  "SPXW", expiry, quote.call_wing.long_leg.strike,  "C"),
        ]

        if self._is_paper:
            return self._paper_fill(
                "iron_condor", size.contracts,
                quote.net_credit, size.max_loss_per_contract * size.contracts,
            )

        return await self._submit_combo_order(
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
        # Live: would mirror the entry legs with reversed BUY/SELL
        # Implemented when live mode is activated (Phase 3)
        raise NotImplementedError("Live close not yet implemented — still in paper mode")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _submit_combo_order(
        self,
        spread_type: str,
        legs: list[OrderLeg],
        net_credit: float,
        contracts: int,
        max_loss_dollars: float,
    ) -> OrderResult:
        """POST a multi-leg combo order to Webull."""
        import aiohttp

        session = await self._get_session()
        payload = {
            "accountId": config.WEBULL_ACCOUNT_ID,
            "orderType": "LMT",
            "timeInForce": "DAY",
            "comboType": "VERTICAL" if len(legs) == 2 else "CONDOR",
            "lmtPrice": str(round(net_credit, 2)),
            "qty": str(contracts),
            "legs": [
                {
                    "action": leg.action,
                    "symbol": leg.symbol,
                    "expDate": leg.exp_date,
                    "strike": str(int(leg.strike)),
                    "right": leg.right,
                    "ratio": leg.ratio,
                }
                for leg in legs
            ],
        }
        headers = {
            "App": "desktop",
            "App-Group": "broker",
            "Appid": config.WEBULL_APP_KEY,
            "Did": config.WEBULL_ACCOUNT_ID,
            "Access-Token": config.WEBULL_TRADE_TOKEN,
            "Content-Type": "application/json",
        }

        url = f"{ORDER_BASE_URL}/order/place"
        LOGGER.info("Submitting LIVE order: %s × %d @ %.2f credit", spread_type, contracts, net_credit)

        try:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            LOGGER.error("Order submission failed: %s", exc)
            return self._rejected(spread_type, contracts, net_credit, f"http_error: {exc}")

        order_id = str(data.get("orderId") or data.get("data", {}).get("orderId", ""))
        status_str = str(data.get("status") or data.get("data", {}).get("status", "PENDING")).upper()

        try:
            status = OrderStatus[status_str]
        except KeyError:
            status = OrderStatus.PENDING

        return OrderResult(
            order_id=order_id or str(uuid.uuid4()),
            status=status,
            spread_type=spread_type,
            contracts=contracts,
            net_credit=net_credit,
            net_credit_dollars=net_credit * contracts * SPX_MULTIPLIER,
            max_loss_dollars=max_loss_dollars,
            filled_at=datetime.now(EASTERN) if status == OrderStatus.FILLED else None,
            is_paper=False,
            raw_response=data,
        )

    def _paper_fill(
        self, spread_type: str, contracts: int, net_credit: float, max_loss_dollars: float,
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
        self, spread_type: str, contracts: int, net_credit: float, reason: str,
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
        return datetime.now(EASTERN).strftime("%Y%m%d")

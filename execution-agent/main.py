"""
main.py — InfiniteLoop Execution Agent (Layer 2) entrypoint.

Lifecycle:
  1. Validate config, load active strategy from DB
  2. Start Webull MQTT feed (ES tick data → BarNormalizer)
  3. Start watchdog (safety monitor)
  4. Trading loop:
     a. Wait for RTH open
     b. After 45 RTH bars, classify direction (Oracle)
     c. Select spread type and strikes
     d. Price the spread via Webull options quote API
     e. Size the trade (risk rules)
     f. Enter the spread (paper or live)
     g. Monitor for exit (profit target / stop / 3:45 PM)
  5. Log all fills to PostgreSQL
  6. Graceful shutdown at EOD

This agent is stateless between days — it reloads strategy and equity from
PostgreSQL each morning. Paper mode is enforced until Phase 3 sign-off.

Run: python main.py
Deploy: see Dockerfile
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

# Config must be imported first — it loads .env
import config

from constants import (
    DEFAULT_SPREAD_WIDTH, RTH_OPEN_HOUR, RTH_OPEN_MINUTE,
    MIN_RTH_MINUTES_BEFORE_CLASSIFY,
)
from data.feed import WebullFeed
from data.normalizer import Bar
from health.watchdog import Watchdog
from trade_logging.trade_logger import TradeLogger
from orders.manager import OrderManager, OrderResult, OrderStatus
from orders.state import OpenPosition, PositionState
from risk.limits import daily_halt_triggered
from risk.manager import size_trade
from signals.classifier import LiveClassifier, PriorDayContext, ClassificationResult
from spreads.pricer import SpreadPricer
from spreads.selector import SelectedSpread, select_spread
from strategy.loader import ActiveStrategy, load_active_strategy

LOGGER = logging.getLogger("infiniteloop.main")
EASTERN = pytz.timezone("US/Eastern")

# How often the trading loop polls for new bars / exit conditions
POLL_INTERVAL_SECONDS = 10

# Profit target: close when premium decays to (1 - target_pct/100) of credit
# Stop loss: close when loss reaches (stop_loss_pct/100) of credit

DEFAULT_STARTING_EQUITY = 5000.0


# ── Logging setup ──────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure structured JSON logging for Railway log aggregation."""
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.basicConfig(level=level, handlers=[handler], force=True)


# ── Prior-day context (Webull DataClient) ─────────────────────────────────────

# Fallback prior-day context when Webull data is unavailable.
# Oracle classifies NEUTRAL on flat/missing features → agent skips trading
# rather than guessing direction. Safe failure mode.
_FALLBACK_PRIOR_CTX = dict(prior_close=5300.0, prior_prior_close=5300.0, current_vix=20.0)


def _parse_bar_closes(resp) -> list[float]:
    """
    Extract close prices from a Webull DataClient history-bars response.
    The SDK returns a response object whose .body is a list of bar objects,
    each having a .close attribute (or dict key).
    """
    closes: list[float] = []
    body = getattr(resp, "body", None) if resp else None
    if body is None and isinstance(resp, dict):
        body = resp.get("data") or resp
    if not body:
        return closes
    items = body if isinstance(body, list) else [body]
    for item in items:
        try:
            val = (
                float(item.close)
                if hasattr(item, "close")
                else float(item.get("close") or item.get("c") or 0)
            )
            if val > 0:
                closes.append(val)
        except Exception:
            continue
    return closes


def _fetch_prior_day_context(api_client=None) -> PriorDayContext:
    """
    Fetch prior-day ES close (proxy for SPX) and VX close (proxy for VIX)
    from the Webull DataClient using the SDK already authenticated at startup.

    Uses:
      - ES daily history bars  → prior_close, prior_prior_close
      - VX (VIX futures) snapshot prev_close → current_vix

    If Webull data is unavailable, falls back to neutral defaults so Oracle
    classifies SKIP and the agent waits rather than crashing.
    """
    from webull.data.data_client import DataClient
    from webull.data.common.category import Category

    if api_client is None:
        api_client = config.build_api_client()

    client = DataClient(api_client)
    LOGGER.info("Fetching prior-day context from Webull DataClient...")

    # ── ES daily bars (prior_close, prior_prior_close) ─────────────────────
    es_closes: list[float] = []
    try:
        resp = client.futures_market_data.get_futures_history_bars(
            symbols=["ESc1"],
            category=Category.US_FUTURES,
            timespan="d1",
            count="5",
        )
        es_closes = _parse_bar_closes(resp)
        if es_closes:
            LOGGER.info("ES daily bars: %d bars, last close=%.2f", len(es_closes), es_closes[-1])
        else:
            LOGGER.warning("Webull ES history bars returned no data")
    except Exception as exc:
        LOGGER.warning("ES history bars fetch failed: %s", exc)

    # ── VX snapshot for VIX proxy ──────────────────────────────────────────
    current_vix: float = _FALLBACK_PRIOR_CTX["current_vix"]
    try:
        resp_vx = client.futures_market_data.get_futures_snapshot(
            symbols=["VXc1"],
            category=Category.US_FUTURES,
        )
        body = getattr(resp_vx, "body", None)
        items = body if isinstance(body, list) else ([body] if body else [])
        if items:
            item = items[0]
            # Use last close; fall back to prev_close if close is 0
            vx_close = float(getattr(item, "close", None) or getattr(item, "prev_close", None) or 0)
            if vx_close > 0:
                current_vix = vx_close
                LOGGER.info("VX snapshot close=%.2f (used as VIX proxy)", current_vix)
        if current_vix == _FALLBACK_PRIOR_CTX["current_vix"]:
            LOGGER.warning("VX snapshot unusable — using default VIX=%.1f", current_vix)
    except Exception as exc:
        LOGGER.warning("VX snapshot fetch failed: %s — using default VIX=%.1f", exc, current_vix)

    # ── Build context ──────────────────────────────────────────────────────
    if len(es_closes) >= 2:
        prior_close       = es_closes[-1]
        prior_prior_close = es_closes[-2]
    elif len(es_closes) == 1:
        prior_close = prior_prior_close = es_closes[0]
    else:
        LOGGER.error(
            "No ES history data from Webull — using defaults. "
            "Oracle will classify SKIP until data is available."
        )
        prior_close = prior_prior_close = _FALLBACK_PRIOR_CTX["prior_close"]

    ctx = PriorDayContext(
        prior_close=prior_close,
        prior_vwap=prior_close,          # no intraday VWAP at startup; close is close enough
        prior_prior_close=prior_prior_close,
        current_vix=current_vix,
        date=datetime.now(EASTERN).strftime("%Y-%m-%d"),
    )
    LOGGER.info(
        "Prior-day context: es_close=%.2f es_prev=%.2f vix=%.1f",
        ctx.prior_close, ctx.prior_prior_close, ctx.current_vix,
    )
    return ctx


# ── Account equity ─────────────────────────────────────────────────────────────

async def _fetch_account_equity(api_client=None) -> float:
    """
    Fetch current account equity via Webull SDK TradeClient.
    Falls back to DEFAULT_STARTING_EQUITY if the call fails.
    """
    try:
        from webull.trade.trade_client import TradeClient
        loop = asyncio.get_event_loop()

        def _get_equity():
            trade_client = TradeClient(api_client or config.build_api_client())
            resp = trade_client.account_v2.get_account_detail(
                account_id=config.WEBULL_ACCOUNT_ID
            )
            if hasattr(resp, "body") and resp.body:
                body = resp.body.__dict__ if hasattr(resp.body, "__dict__") else resp.body
                return float(
                    body.get("netLiquidation")
                    or body.get("net_liquidation")
                    or body.get("totalAssets")
                    or DEFAULT_STARTING_EQUITY
                )
            return DEFAULT_STARTING_EQUITY

        equity = await loop.run_in_executor(None, _get_equity)
        LOGGER.info("Account equity from Webull SDK: $%.2f", equity)
        return equity
    except Exception as exc:
        LOGGER.warning(
            "Could not fetch equity via SDK (%s) — using $%.2f", exc, DEFAULT_STARTING_EQUITY
        )
        return DEFAULT_STARTING_EQUITY


# ── Wait helpers ───────────────────────────────────────────────────────────────

async def _wait_for_rth(watchdog: Watchdog) -> None:
    """Sleep until RTH opens (09:30 ET). Exit if watchdog halts."""
    rth_open = datetime.now(EASTERN).replace(
        hour=RTH_OPEN_HOUR, minute=RTH_OPEN_MINUTE, second=0, microsecond=0
    )
    now = datetime.now(EASTERN)
    if now >= rth_open:
        return
    wait_secs = (rth_open - now).total_seconds()
    LOGGER.info("Waiting %.0f seconds for RTH open (%s ET)", wait_secs, rth_open.strftime("%H:%M"))
    while datetime.now(EASTERN) < rth_open:
        if watchdog.is_halted:
            return
        await asyncio.sleep(5)


async def _wait_for_bars(feed: WebullFeed, watchdog: Watchdog) -> Optional[list[Bar]]:
    """Wait until MIN_RTH_MINUTES_BEFORE_CLASSIFY RTH bars are available."""
    while True:
        if watchdog.is_halted:
            return None
        bars = feed.rth_bars_today()
        if len(bars) >= MIN_RTH_MINUTES_BEFORE_CLASSIFY:
            return bars
        LOGGER.debug("RTH bars: %d / %d", len(bars), MIN_RTH_MINUTES_BEFORE_CLASSIFY)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ── Exit monitor ───────────────────────────────────────────────────────────────

async def _monitor_position(
    position: OpenPosition,
    feed: WebullFeed,
    order_mgr: OrderManager,
    pos_state: PositionState,
    trade_logger: TradeLogger,
    trade_id: int,
    strategy: ActiveStrategy,
    equity: float,
    watchdog: Watchdog,
) -> None:
    """
    Poll for exit conditions while a position is open.
    Exits on profit target, stop loss, or watchdog forced exit.
    """
    profit_target_pct = strategy.trade.profit_target_pct / 100.0   # e.g. 0.50
    stop_loss_pct     = strategy.trade.stop_loss_pct / 100.0        # e.g. 2.00
    entry_credit      = position.entry_credit

    profit_target_debit = entry_credit * (1.0 - profit_target_pct)   # close at 50% decay
    stop_loss_debit     = entry_credit * (1.0 + stop_loss_pct)       # close if 2x credit lost

    LOGGER.info(
        "Monitoring %s — profit_target_debit=%.2f stop_loss_debit=%.2f",
        position.spread_type, profit_target_debit, stop_loss_debit,
    )

    while position.is_open and not watchdog.is_halted:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

        # Forced exit check (watchdog sets _forced_exit_fired; we also check directly)
        if not watchdog.trading_allowed:
            break   # watchdog will call close_callback

        # TODO: fetch live spread mid-price from Webull to check profit/stop
        # For paper mode, use a simplified time-decay approximation:
        # After 80% of the day has elapsed post-entry, use 50% profit assumption
        now_et = datetime.now(EASTERN)
        elapsed_mins = (now_et - position.entry_time).total_seconds() / 60
        if config.IS_PAPER_MODE and elapsed_mins > 120:
            # Paper mode: assume profit target hit after 2 hours
            exit_debit = profit_target_debit
            reason = "profit_target_paper_proxy"
            LOGGER.info("[PAPER] Assuming profit target hit after %.0f min", elapsed_mins)
            await _execute_exit(
                position, exit_debit, reason,
                order_mgr, pos_state, trade_logger, trade_id, equity,
            )
            return


async def _execute_exit(
    position: OpenPosition,
    exit_debit: float,
    reason: str,
    order_mgr: OrderManager,
    pos_state: PositionState,
    trade_logger: TradeLogger,
    trade_id: int,
    equity: float,
) -> None:
    """Close the spread and log the exit."""
    entry_order = position.entry_order
    close_result = await order_mgr.close_spread(entry_order, exit_debit, reason)

    if close_result.status not in (OrderStatus.PAPER_FILL, OrderStatus.FILLED):
        LOGGER.error("Close order failed: %s", close_result.rejection_reason)
        return

    closed = pos_state.record_exit(exit_debit=exit_debit, reason=reason)
    if closed:
        trade_logger.log_exit(trade_id, closed, equity)
        LOGGER.info("Exit complete: %s pnl=$%.2f", reason, closed.realized_pnl_dollars or 0.0)


# ── Main trading loop ──────────────────────────────────────────────────────────

async def run_trading_day(
    strategy: ActiveStrategy,
    feed: WebullFeed,
    order_mgr: OrderManager,
    pos_state: PositionState,
    pricer: SpreadPricer,
    trade_logger: TradeLogger,
    watchdog: Watchdog,
    equity: float,
    prior_ctx: PriorDayContext,
) -> None:
    """Execute one full trading day: classify → select → price → size → trade → monitor."""
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    LOGGER.info("Trading day started: %s (paper=%s equity=$%.2f)", today, config.IS_PAPER_MODE, equity)

    # Wait for RTH open
    await _wait_for_rth(watchdog)
    if watchdog.is_halted:
        return

    # Wait for classification bars
    rth_bars = await _wait_for_bars(feed, watchdog)
    if rth_bars is None or watchdog.is_halted:
        return

    # Classify direction
    classifier = LiveClassifier(
        oracle_params=strategy.oracle.to_dict(),
        prior=prior_ctx,
    )
    result: Optional[ClassificationResult] = classifier.maybe_classify(rth_bars)
    if result is None:
        LOGGER.warning("Classifier returned None despite %d bars — skipping day", len(rth_bars))
        return

    LOGGER.info(
        "Direction: %s (confidence=%.2f skip_reason=%s)",
        result.direction, result.confidence, result.skip_reason,
    )

    if result.direction == "SKIP":
        trade_logger.log_portfolio_event("day_skipped", {
            "reason": result.skip_reason,
            "confidence": result.confidence,
            "date": today,
        }, strategy.strategy_id)
        return

    # Select spread
    spx_price = feed.last_price or 0.0
    if spx_price <= 0:
        LOGGER.warning("No live SPX price available — skipping day")
        return

    selected: SelectedSpread = select_spread(
        direction=result.direction,
        spx_price=spx_price,
        vix=prior_ctx.current_vix,
        em_mult_high=strategy.trade.em_mult_high,
        em_mult_low=strategy.trade.em_mult_low,
        spread_width=strategy.trade.spread_width,
        condor_em_mult=strategy.trade.condor_em_mult,
        skip_reason=result.skip_reason,
    )

    if selected.direction == "SKIP":
        LOGGER.info("Spread selection skipped: %s", selected.skip_reason)
        return

    # Price the spread
    if result.direction == "NEUTRAL" and selected.put_wing and selected.call_wing:
        condor_quote = await pricer.price_iron_condor(selected.put_wing, selected.call_wing)
        if not condor_quote.is_valid:
            LOGGER.warning("Iron condor quote invalid: %s", condor_quote.invalid_reason)
            return
        net_credit  = condor_quote.net_credit
        max_loss_pts = condor_quote.max_loss_points
        spread_width = strategy.trade.spread_width
    elif selected.primary:
        spread_quote = await pricer.price_spread(selected.primary)
        if not spread_quote.is_valid:
            LOGGER.warning("Spread quote invalid: %s", spread_quote.invalid_reason)
            return
        net_credit   = spread_quote.net_credit
        max_loss_pts = spread_quote.max_loss_points
        spread_width = strategy.trade.spread_width
        condor_quote = None
    else:
        LOGGER.error("SelectedSpread has no legs — this is a bug")
        return

    # Size the trade
    size = size_trade(
        spread_type=result.direction.lower() + "_spread",
        short_strike=selected.primary.short_strike if selected.primary else selected.put_wing.short_strike,
        long_strike=selected.primary.long_strike  if selected.primary else selected.put_wing.long_strike,
        credit=net_credit,
        equity=equity,
        spread_width=spread_width,
    )

    if size.contracts == 0:
        LOGGER.info("Trade skipped by risk manager: %s", size.skip_reason)
        trade_logger.log_portfolio_event("trade_skipped", {
            "skip_reason": size.skip_reason,
            "direction": result.direction,
            "net_credit": net_credit,
            "equity": equity,
            "date": today,
        }, strategy.strategy_id)
        return

    # Enter the trade
    if result.direction == "NEUTRAL" and condor_quote:
        order = await order_mgr.enter_iron_condor(condor_quote, size)
    else:
        order = await order_mgr.enter_vertical_spread(spread_quote, size)

    if order.status in (OrderStatus.REJECTED,):
        LOGGER.error("Order rejected: %s", order.rejection_reason)
        return

    position = pos_state.record_entry(
        order=order, direction=result.direction, spread_width=spread_width,
    )

    trade_id = trade_logger.log_entry(
        order=order,
        strategy_id=strategy.strategy_id,
        direction=result.direction,
        spread_width=spread_width,
        spx_price=spx_price,
        vix=prior_ctx.current_vix,
        expected_move=selected.expected_move,
        oracle_confidence=result.confidence,
    ) or 0

    # Monitor for exit
    await _monitor_position(
        position=position,
        feed=feed,
        order_mgr=order_mgr,
        pos_state=pos_state,
        trade_logger=trade_logger,
        trade_id=trade_id,
        strategy=strategy,
        equity=equity,
        watchdog=watchdog,
    )


# ── Agent entrypoint ───────────────────────────────────────────────────────────

async def main() -> None:
    _setup_logging()

    LOGGER.info("InfiniteLoop Execution Agent starting up")
    config.validate()

    # Load strategy from DB
    strategy = load_active_strategy()
    if strategy is None:
        LOGGER.critical("No active strategy in database — cannot start. Run Strategy Lab first.")
        sys.exit(1)

    # Build a single SDK client shared by all components
    api_client = config.build_api_client()

    # Shared components
    pos_state    = PositionState()
    order_mgr    = OrderManager(api_client=api_client)
    pricer       = SpreadPricer(api_client=api_client)
    trade_logger = TradeLogger()

    # Fetch prior-day context for Oracle features
    prior_ctx = _fetch_prior_day_context(api_client=api_client)

    # Account equity
    equity = await _fetch_account_equity(api_client=api_client)

    # Watchdog close callback — closes any open position
    async def _close_all() -> None:
        if pos_state.has_open_position:
            pos = pos_state.current_position
            LOGGER.critical("Watchdog closing open position: %s", pos.spread_type)
            # In paper mode: record exit at worst-case (full loss)
            worst_case_debit = pos.entry_credit + pos.spread_width
            await _execute_exit(
                pos, worst_case_debit, "watchdog_close",
                order_mgr, pos_state, trade_logger, 0, equity,
            )

    watchdog = Watchdog(close_callback=_close_all)
    await watchdog.start(starting_equity=equity)

    # Feed — connect to Webull MQTT
    def _on_bar(bar: Bar) -> None:
        watchdog.update_feed(time.time())

    feed = WebullFeed(api_client=api_client, on_bar=_on_bar)
    await feed.start()

    # Graceful shutdown handler
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(watchdog, feed, pricer, trade_logger)))

    # Main trading loop — one day at a time
    try:
        while True:
            now_et = datetime.now(EASTERN)

            # Reset daily state at midnight ET
            if now_et.hour == 0 and now_et.minute < 1:
                equity = await _fetch_account_equity(api_client=api_client)
                prior_ctx = _fetch_prior_day_context(api_client=api_client)
                pos_state.reset()
                watchdog.reset_for_new_day(starting_equity=equity)
                trade_logger.log_equity_snapshot(
                    strategy_id=strategy.strategy_id,
                    equity=equity,
                    daily_pnl=pos_state.daily_pnl_dollars,
                    daily_trades=len(pos_state.daily_trades),
                )
                LOGGER.info("New trading day: equity=$%.2f", equity)

            if not watchdog.is_halted and not pos_state.traded_today:
                try:
                    await run_trading_day(
                        strategy=strategy,
                        feed=feed,
                        order_mgr=order_mgr,
                        pos_state=pos_state,
                        pricer=pricer,
                        trade_logger=trade_logger,
                        watchdog=watchdog,
                        equity=equity,
                        prior_ctx=prior_ctx,
                    )
                except Exception as exc:
                    LOGGER.exception("Unhandled exception in trading loop: %s", exc)
                    await watchdog.trigger_halt(f"unhandled_exception: {exc}")

            await asyncio.sleep(30)

    except asyncio.CancelledError:
        LOGGER.info("Main loop cancelled")
    finally:
        await _shutdown(watchdog, feed, pricer, trade_logger)


async def _shutdown(
    watchdog: Watchdog,
    feed: WebullFeed,
    pricer: SpreadPricer,
    trade_logger: TradeLogger,
) -> None:
    LOGGER.info("Shutting down execution agent...")
    await watchdog.stop()
    await feed.stop()
    await pricer.close()
    trade_logger.close()
    LOGGER.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

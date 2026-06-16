"""
health/watchdog.py — Dead man's switch and 3:45 PM forced exit monitor.

The watchdog runs as an asyncio task alongside the main trading loop. It monitors:
  1. Feed health — if no tick arrives in FEED_STALL_SECONDS, halt and go flat
  2. Daily loss limit — if daily P&L ≤ -5% of starting equity, halt for the day
  3. Forced exit — at 3:45 PM ET, close any open spread unconditionally
  4. Unhandled exceptions — caught in main.py, forwarded to watchdog.trigger_halt()

On halt: the watchdog sets _halt_flag, logs CRITICAL, and calls the registered
close_callback (which closes any open position via the order manager).

NON-NEGOTIABLE: The 3:45 PM forced exit is hard-coded. It is not configurable.
0DTE gamma spikes in the last 15 minutes can be catastrophic — we always exit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import Callable, Coroutine, Optional

import pytz

from constants import (
    FORCED_EXIT_HOUR, FORCED_EXIT_MINUTE,
    MAX_DAILY_LOSS_PCT,
)

LOGGER = logging.getLogger("infiniteloop.health.watchdog")

EASTERN = pytz.timezone("US/Eastern")

# If no tick arrives for this many seconds, the feed is considered stalled
FEED_STALL_SECONDS = 120   # 2 minutes
# How often the watchdog loop checks all conditions
CHECK_INTERVAL_SECONDS = 10

# Forced exit window — we start trying at FORCED_EXIT_HOUR:FORCED_EXIT_MINUTE ET
FORCED_EXIT_TIME = time(FORCED_EXIT_HOUR, FORCED_EXIT_MINUTE)


class Watchdog:
    """
    Async watchdog that enforces hard safety rules.

    Usage in main.py:
        async def do_close():
            await order_mgr.close_spread(...)
        watchdog = Watchdog(close_callback=do_close)
        await watchdog.start(starting_equity=5000.0)
        ...
        watchdog.update_feed(last_tick_at)
        watchdog.update_equity(current_equity)
        ...
        await watchdog.stop()
    """

    def __init__(
        self,
        close_callback: Callable[[], Coroutine],
        notify_callback: Optional[Callable[[str, str], Coroutine]] = None,
    ) -> None:
        """
        Args:
            close_callback:  async function to close any open position (go flat)
            notify_callback: optional async function(reason, detail) for alerts
        """
        self._close_callback  = close_callback
        self._notify_callback = notify_callback
        self._task: Optional[asyncio.Task] = None
        self._halt_flag = False
        self._forced_exit_fired = False
        self._starting_equity: float = 0.0
        self._current_equity:  float = 0.0
        self._last_tick_at:    Optional[float] = None
        self._halt_reason:     str = ""

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self, starting_equity: float) -> None:
        self._starting_equity = starting_equity
        self._current_equity  = starting_equity
        self._halt_flag = False
        self._forced_exit_fired = False
        self._task = asyncio.create_task(self._run(), name="watchdog")
        LOGGER.info("Watchdog started (starting_equity=$%.2f)", starting_equity)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        LOGGER.info("Watchdog stopped")

    def update_feed(self, last_tick_at: Optional[float]) -> None:
        """Call whenever a tick arrives. `last_tick_at` is a Unix timestamp."""
        self._last_tick_at = last_tick_at

    def update_equity(self, current_equity: float) -> None:
        """Call after each trade fill to keep equity current."""
        self._current_equity = current_equity

    async def trigger_halt(self, reason: str) -> None:
        """
        External halt trigger (called from main.py on unhandled exception).
        Idempotent — safe to call multiple times.
        """
        if not self._halt_flag:
            self._halt_reason = reason
            await self._halt(reason)

    @property
    def is_halted(self) -> bool:
        return self._halt_flag

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def trading_allowed(self) -> bool:
        """False when halted or after forced exit has fired."""
        return not self._halt_flag and not self._forced_exit_fired

    def reset_for_new_day(self, starting_equity: float) -> None:
        """Call at the start of each trading day."""
        self._starting_equity   = starting_equity
        self._current_equity    = starting_equity
        self._halt_flag         = False
        self._forced_exit_fired = False
        self._halt_reason       = ""
        LOGGER.info("Watchdog reset for new day (starting_equity=$%.2f)", starting_equity)

    # ── Internal loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.error("Watchdog internal error: %s", exc)

    async def _check_all(self) -> None:
        if self._halt_flag:
            return   # already halted

        now_et = datetime.now(EASTERN)

        # 1. Forced exit at 15:45 ET — NON-NEGOTIABLE
        if not self._forced_exit_fired and now_et.time() >= FORCED_EXIT_TIME:
            await self._forced_exit(now_et)
            return

        # 2. Daily loss limit
        if self._starting_equity > 0:
            daily_loss_pct = (self._current_equity - self._starting_equity) / self._starting_equity
            if daily_loss_pct <= -MAX_DAILY_LOSS_PCT:
                await self._halt(
                    f"daily_loss_limit: {daily_loss_pct:.2%} "
                    f"(current=${self._current_equity:.2f}, start=${self._starting_equity:.2f})"
                )
                return

        # 3. Feed stall check (only during RTH: 09:30–16:00 ET)
        rth_open  = datetime.now(EASTERN).replace(hour=9,  minute=30, second=0, microsecond=0)
        rth_close = datetime.now(EASTERN).replace(hour=16, minute=0,  second=0, microsecond=0)
        if rth_open <= now_et < rth_close:
            import time as _time
            if self._last_tick_at is not None:
                stale_secs = _time.time() - self._last_tick_at
                if stale_secs > FEED_STALL_SECONDS:
                    await self._halt(
                        f"feed_stall: no tick for {stale_secs:.0f}s (limit={FEED_STALL_SECONDS}s)"
                    )
                    return

    async def _forced_exit(self, now_et: datetime) -> None:
        """3:45 PM ET hard exit — close everything regardless of P&L."""
        self._forced_exit_fired = True
        LOGGER.critical(
            "FORCED EXIT at %s ET — all 0DTE positions must be closed before expiration.",
            now_et.strftime("%H:%M:%S"),
        )
        await self._do_close("forced_exit_3:45pm")
        if self._notify_callback:
            try:
                await self._notify_callback("FORCED_EXIT", f"Forced exit at {now_et.strftime('%H:%M')} ET")
            except Exception as exc:
                LOGGER.error("Notify callback failed: %s", exc)

    async def _halt(self, reason: str) -> None:
        """Set halt flag, go flat, and notify."""
        self._halt_flag = True
        self._halt_reason = reason
        LOGGER.critical("WATCHDOG HALT: %s", reason)
        await self._do_close(reason)
        if self._notify_callback:
            try:
                await self._notify_callback("HALT", reason)
            except Exception as exc:
                LOGGER.error("Notify callback failed after halt: %s", exc)

    async def _do_close(self, reason: str) -> None:
        """Call the registered close callback. Swallow errors — we must not hang."""
        try:
            await self._close_callback()
            LOGGER.info("Close callback completed (reason=%s)", reason)
        except Exception as exc:
            LOGGER.critical(
                "CRITICAL: Close callback failed during %s: %s — MANUAL INTERVENTION REQUIRED",
                reason, exc,
            )

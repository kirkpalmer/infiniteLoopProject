"""
data/normalizer.py — Convert raw Webull MQTT tick events into 1-minute OHLCV bars.

The feed module calls on_tick() for every incoming price update. This module
accumulates ticks and closes each 1-minute bar at the top of the next minute,
keeping the last N bars in a rolling deque for the signal classifier.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import pytz

LOGGER = logging.getLogger("infiniteloop.data.normalizer")

EASTERN = pytz.timezone("US/Eastern")
MAX_BARS = 120   # keep 2 hours of 1-min bars in memory


@dataclass
class Bar:
    """One 1-minute OHLCV bar (US/Eastern timestamps)."""
    timestamp: datetime   # bar open time, US/Eastern, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class _OpenBar:
    """Accumulator for the bar currently being built."""
    minute_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def update(self, price: float, size: int) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size

    def to_bar(self) -> Bar:
        return Bar(
            timestamp=self.minute_start,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class BarNormalizer:
    """
    Stateful tick-to-bar converter.

    Usage:
        norm = BarNormalizer(on_bar=my_callback)
        norm.on_tick(price=5750.25, size=10, ts=datetime.now(timezone.utc))
    """

    def __init__(
        self,
        on_bar: Optional[Callable[[Bar], None]] = None,
        max_bars: int = MAX_BARS,
    ) -> None:
        self._on_bar = on_bar
        self._current: Optional[_OpenBar] = None
        self.bars: deque[Bar] = deque(maxlen=max_bars)

    def on_tick(self, price: float, size: int, ts: datetime) -> None:
        """
        Process one tick. `ts` should be tz-aware (UTC is fine; converted internally).
        Emits a completed Bar via on_bar() whenever a minute boundary is crossed.
        """
        et = ts.astimezone(EASTERN)
        minute_start = et.replace(second=0, microsecond=0)

        if self._current is None:
            self._current = _OpenBar(
                minute_start=minute_start,
                open=price, high=price, low=price, close=price,
            )
            self._current.update(price, size)
            return

        if minute_start > self._current.minute_start:
            # Close the old bar and start a new one
            completed = self._current.to_bar()
            self.bars.append(completed)
            if self._on_bar is not None:
                try:
                    self._on_bar(completed)
                except Exception as exc:
                    LOGGER.error("on_bar callback raised: %s", exc)

            self._current = _OpenBar(
                minute_start=minute_start,
                open=price, high=price, low=price, close=price,
            )
            self._current.update(price, size)
        else:
            self._current.update(price, size)

    def latest_bars(self, n: int = 60) -> list[Bar]:
        """Return the last n completed bars (oldest first)."""
        bars = list(self.bars)
        return bars[-n:]

    def rth_bars_today(self, date: Optional[datetime] = None) -> list[Bar]:
        """Return all completed RTH bars (09:30–16:00 ET) for the given date."""
        if date is None:
            date = datetime.now(EASTERN)
        target_date = date.astimezone(EASTERN).date()

        result = []
        for bar in self.bars:
            bar_date = bar.timestamp.astimezone(EASTERN).date()
            bar_time = bar.timestamp.astimezone(EASTERN).time()
            if (
                bar_date == target_date
                and bar_time >= datetime.strptime("09:30", "%H:%M").time()
                and bar_time < datetime.strptime("16:00", "%H:%M").time()
            ):
                result.append(bar)
        return result

    @property
    def last_price(self) -> Optional[float]:
        """Most recent trade price (from the open bar or last completed bar)."""
        if self._current is not None:
            return self._current.close
        if self.bars:
            return self.bars[-1].close
        return None

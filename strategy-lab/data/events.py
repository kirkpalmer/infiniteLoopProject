"""Economic event calendar for InfiniteLoop. Identifies macro event days
where the direction classifier is unreliable (FOMC, CPI, NFP, PCE). On these days the
system skips trading regardless of the morning signal. Updated annually."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Final

LOGGER = logging.getLogger("infiniteloop.data.events")


def _first_weekday_of_month(year: int, month: int, weekday: int) -> date:
    first_day = date(year, month, 1)
    offset = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=offset)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    current = next_month - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _second_wednesday(year: int, month: int) -> date:
    first_wednesday = _first_weekday_of_month(year, month, 2)
    return first_wednesday + timedelta(days=7)


def _fomc_announcement_dates() -> set[date]:
    return {
        date(2025, 1, 29),
        date(2025, 3, 19),
        date(2025, 5, 7),
        date(2025, 6, 18),
        date(2025, 7, 30),
        date(2025, 9, 17),
        date(2025, 10, 29),
        date(2025, 12, 10),
        date(2026, 1, 28),
        date(2026, 3, 18),
        date(2026, 4, 29),
        date(2026, 6, 17),
        date(2026, 7, 29),
        date(2026, 9, 16),
        date(2026, 10, 28),
        date(2026, 12, 9),
    }


def _monthly_dates(years: tuple[int, ...], builder) -> set[date]:
    months = range(1, 13)
    return {builder(year, month) for year in years for month in months}


FOMC_DATES: Final[set[date]] = _fomc_announcement_dates()
CPI_DATES: Final[set[date]] = _monthly_dates((2025, 2026), _second_wednesday)
NFP_DATES: Final[set[date]] = _monthly_dates((2025, 2026), lambda year, month: _first_weekday_of_month(year, month, 4))
PCE_DATES: Final[set[date]] = _monthly_dates((2025, 2026), lambda year, month: _last_weekday_of_month(year, month, 4))
ALL_EVENT_DATES: Final[set[date]] = FOMC_DATES | CPI_DATES | NFP_DATES | PCE_DATES


def is_event_day(value: date) -> bool:
    """Return True when the supplied date is a macro event day."""

    return value in ALL_EVENT_DATES


def is_post_event_day(value: date) -> bool:
    """Return True when the previous trading day was an event day."""

    previous_day = value - timedelta(days=1)
    while previous_day.weekday() >= 5:
        previous_day -= timedelta(days=1)
    return previous_day in ALL_EVENT_DATES


def get_event_name(value: date) -> str:
    """Return the event type label for a date, if any."""

    if value in FOMC_DATES:
        return "FOMC"
    if value in CPI_DATES:
        return "CPI"
    if value in NFP_DATES:
        return "NFP"
    if value in PCE_DATES:
        return "PCE"
    if is_post_event_day(value):
        return "POST_EVENT"
    return ""

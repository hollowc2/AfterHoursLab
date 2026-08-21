"""Shared trading-day arithmetic: weekend-skipping previous/next trading day.

Used by archive_earnings.py to size the day_before/after_hours/day_after capture
window around an earnings_date, and by capture.py to work out which window "today"
falls into for a given earnings_date.
"""

from __future__ import annotations

import datetime as dt

_WEEKEND = (5, 6)


def previous_trading_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() in _WEEKEND:
        day -= dt.timedelta(days=1)
    return day


def next_trading_day(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() in _WEEKEND:
        day += dt.timedelta(days=1)
    return day


def latest_trading_day(day: dt.date) -> dt.date:
    """The most recent trading day on or before `day` — `day` itself on a weekday,
    else the preceding Friday.

    Used to judge watchlist freshness: a watchlist written last Friday is current,
    not stale, when read on the following Sunday, because no archive run was
    scheduled in between.
    """
    while day.weekday() in _WEEKEND:
        day -= dt.timedelta(days=1)
    return day

"""Shared XNYS session calendar and trading-day arithmetic.

Used by archive_earnings.py to size the day_before/after_hours/day_after capture
window around an earnings_date, and by capture.py to work out which window "today"
falls into for a given earnings_date.
"""

from __future__ import annotations

import datetime as dt
from importlib.metadata import version

import exchange_calendars as xcals

CALENDAR_NAME = "XNYS"
CALENDAR_VERSION = version("exchange-calendars")
_CALENDAR = xcals.get_calendar(CALENDAR_NAME)


def is_trading_day(day: dt.date) -> bool:
    return bool(_CALENDAR.is_session(day.isoformat()))


def regular_session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return the authoritative regular-session [open, close) bounds in UTC."""
    label = day.isoformat()
    if not _CALENDAR.is_session(label):
        raise ValueError(f"{day} is not an {CALENDAR_NAME} trading session")
    return (
        _CALENDAR.session_open(label).to_pydatetime(),
        _CALENDAR.session_close(label).to_pydatetime(),
    )


def previous_trading_day(day: dt.date) -> dt.date:
    prior = day - dt.timedelta(days=1)
    return _CALENDAR.date_to_session(prior.isoformat(), direction="previous").date()


def next_trading_day(day: dt.date) -> dt.date:
    following = day + dt.timedelta(days=1)
    return _CALENDAR.date_to_session(following.isoformat(), direction="next").date()


def latest_trading_day(day: dt.date) -> dt.date:
    """The most recent XNYS session on or before `day`.

    Used to judge watchlist freshness: a watchlist written last Friday is current,
    not stale, when read on the following Sunday, because no archive run was
    scheduled in between.
    """
    return _CALENDAR.date_to_session(day.isoformat(), direction="previous").date()

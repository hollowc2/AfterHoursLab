import datetime as dt

import pytest

from afterhours_lab.trading_calendar import (
    is_trading_day,
    latest_trading_day,
    next_trading_day,
    previous_trading_day,
    regular_session_bounds,
)


def test_thanksgiving_holiday_is_not_a_session() -> None:
    thanksgiving = dt.date(2026, 11, 26)
    assert not is_trading_day(thanksgiving)
    assert previous_trading_day(thanksgiving) == dt.date(2026, 11, 25)
    assert next_trading_day(thanksgiving) == dt.date(2026, 11, 27)
    assert latest_trading_day(thanksgiving) == dt.date(2026, 11, 25)


def test_day_after_thanksgiving_uses_scheduled_early_close() -> None:
    market_open, market_close = regular_session_bounds(dt.date(2026, 11, 27))
    assert market_open == dt.datetime(2026, 11, 27, 14, 30, tzinfo=dt.UTC)
    assert market_close == dt.datetime(2026, 11, 27, 18, 0, tzinfo=dt.UTC)


def test_regular_bounds_reject_holiday() -> None:
    with pytest.raises(ValueError, match="not an XNYS trading session"):
        regular_session_bounds(dt.date(2026, 11, 26))

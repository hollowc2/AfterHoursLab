from __future__ import annotations

import datetime as dt

import pytest

from afterhours_lab.calendar_reconcile import (
    LOOKUP_RADIUS_DAYS,
    RecordedEvent,
    describe,
    lookup_listings,
    plan_reconciliation,
)
from afterhours_lab.earnings import EarningsCalendarError, EarningsEntry

AUG27 = dt.date(2026, 8, 27)
SEP11 = dt.date(2026, 9, 11)
SEP21 = dt.date(2026, 9, 21)


def recorded(date: dt.date, *, quarter: int | None = 2, superseded: bool = False):
    return RecordedEvent("ANAB", date, 2026 if quarter else None, quarter, superseded)


def listing(date: dt.date, *, quarter: int = 2, hour: str = "amc", symbol: str = "ANAB"):
    return EarningsEntry(symbol=symbol, date=date, hour=hour, year=2026, quarter=quarter)


def test_quarter_listed_on_another_date_supersedes_the_earlier_rows() -> None:
    plan = plan_reconciliation(
        [recorded(AUG27), recorded(SEP11), recorded(SEP21)], [listing(SEP21)]
    )

    assert [(item.earnings_date, item.superseded_by_date) for item in plan.supersede] == [
        (AUG27, SEP21),
        (SEP11, SEP21),
    ]
    assert plan.supersede[0].reason == "earnings calendar lists 2026 Q2 on 2026-09-21"
    assert plan.reinstate == ()


def test_absence_from_the_listings_is_not_evidence() -> None:
    plan = plan_reconciliation([recorded(SEP11)], [listing(SEP21, symbol="OTHER")])
    assert plan.supersede == plan.reinstate == ()


def test_a_different_fiscal_quarter_is_a_different_print() -> None:
    plan = plan_reconciliation([recorded(SEP11)], [listing(dt.date(2026, 11, 2), quarter=3)])
    assert plan.supersede == ()


def test_quarter_listed_on_two_dates_is_ambiguous_and_left_alone() -> None:
    plan = plan_reconciliation([recorded(AUG27)], [listing(SEP11), listing(SEP21)])
    assert plan.supersede == ()


def test_events_without_a_fiscal_quarter_are_never_superseded() -> None:
    plan = plan_reconciliation([recorded(SEP11, quarter=None)], [listing(SEP21)])
    assert plan.supersede == ()


def test_a_move_to_premarket_still_supersedes_the_after_close_row() -> None:
    plan = plan_reconciliation([recorded(SEP11)], [listing(SEP21, hour="bmo")])
    assert [item.earnings_date for item in plan.supersede] == [SEP11]


def test_quarter_moved_back_reinstates_the_row_and_retires_the_other() -> None:
    plan = plan_reconciliation(
        [recorded(SEP11, superseded=True), recorded(SEP21)], [listing(SEP11)]
    )
    assert [event.earnings_date for event in plan.reinstate] == [SEP11]
    assert [item.earnings_date for item in plan.supersede] == [SEP21]


def test_already_superseded_rows_are_not_superseded_again() -> None:
    plan = plan_reconciliation([recorded(SEP11, superseded=True)], [listing(SEP21)])
    assert plan.supersede == plan.reinstate == ()


@pytest.mark.asyncio
async def test_lookup_spans_each_symbols_dates_and_skips_failures() -> None:
    calls = []

    async def lookup(symbol, from_date, to_date):
        calls.append((symbol, from_date, to_date))
        if symbol == "DOWN":
            raise EarningsCalendarError("earnings calendar request timed out")
        return [listing(SEP21), listing(SEP21, symbol="NOISE")]

    listed = await lookup_listings(
        [recorded(AUG27), recorded(SEP11), RecordedEvent("DOWN", SEP11, 2026, 2)], lookup
    )

    radius = dt.timedelta(days=LOOKUP_RADIUS_DAYS)
    assert calls == [
        ("ANAB", AUG27 - radius, SEP11 + radius),
        ("DOWN", SEP11 - radius, SEP11 + radius),
    ]
    assert [entry.symbol for entry in listed] == ["ANAB"]


def test_describe_says_would_under_dry_run() -> None:
    plan = plan_reconciliation(
        [recorded(SEP11), recorded(SEP21, superseded=True)], [listing(SEP21)]
    )

    assert describe(plan) == [
        "superseded ANAB 2026-09-11: earnings calendar lists 2026 Q2 on 2026-09-21",
        "reinstated ANAB 2026-09-21: calendar lists it again",
    ]
    assert describe(plan, dry_run=True) == [
        "would supersede ANAB 2026-09-11: earnings calendar lists 2026 Q2 on 2026-09-21",
        "would reinstate ANAB 2026-09-21: calendar lists it again",
    ]

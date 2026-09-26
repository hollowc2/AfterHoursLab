from __future__ import annotations

import datetime as dt

import pytest
from starlette.datastructures import QueryParams

from afterhours_lab.research import EventFilter
from afterhours_lab.web.params import (
    QueryError,
    describe_filter,
    filter_to_query,
    page_number,
    parse_event_filter,
)


def _params(query: str) -> QueryParams:
    return QueryParams(query)


def test_repeated_and_comma_separated_values_both_parse() -> None:
    repeated = parse_event_filter(_params("symbol=AAPL&symbol=MSFT"))
    joined = parse_event_filter(_params("symbol=aapl,msft"))
    assert repeated.symbols == joined.symbols == ("AAPL", "MSFT")


def test_dates_and_numeric_thresholds_parse() -> None:
    event_filter = parse_event_filter(
        _params("from=2026-01-01&to=2026-06-30&min_move=2.5&max_delay=30&min_volume=1000")
    )
    assert event_filter.date_from == dt.date(2026, 1, 1)
    assert event_filter.date_to == dt.date(2026, 6, 30)
    assert event_filter.min_abs_initial_return == 2.5
    assert event_filter.max_detection_delay_minutes == 30
    assert event_filter.min_volume_105m == 1000


def test_page_becomes_an_offset() -> None:
    event_filter = parse_event_filter(_params("limit=25&page=3"))
    assert event_filter.offset == 50
    assert page_number(event_filter) == 3


def test_bad_date_is_reported_not_swallowed() -> None:
    with pytest.raises(QueryError, match="from must be an ISO date"):
        parse_event_filter(_params("from=last-tuesday"))


def test_bad_number_is_reported() -> None:
    with pytest.raises(QueryError, match="min_move must be a number"):
        parse_event_filter(_params("min_move=lots"))


def test_filter_validation_errors_surface_as_query_errors() -> None:
    with pytest.raises(QueryError, match="unknown reaction_classes"):
        parse_event_filter(_params("class=moon_shot"))
    with pytest.raises(QueryError, match="date_from must be on or before date_to"):
        parse_event_filter(_params("from=2026-06-30&to=2026-01-01"))


def test_out_of_range_page_and_limit_are_rejected() -> None:
    with pytest.raises(QueryError, match="page must be 1 or greater"):
        parse_event_filter(_params("page=0"))
    with pytest.raises(QueryError, match="limit must be between"):
        parse_event_filter(_params("limit=99999"))


def test_query_round_trip_describes_the_same_cohort() -> None:
    original = EventFilter(
        date_from=dt.date(2026, 1, 1),
        date_to=dt.date(2026, 6, 30),
        symbols=("AAPL", "MSFT"),
        reaction_classes=("spike_and_fade",),
        directions=("up",),
        postmarket_collection_modes=("scheduled_capture",),
        min_abs_initial_return=2.0,
        max_retention=0.5,
        min_detection_delay_minutes=2,
        min_coverage_ratio=0.95,
        order_by="retention_asc",
        limit=25,
        offset=50,
    )
    restored = parse_event_filter(_params(filter_to_query(original)))
    assert restored == original


def test_query_overrides_replace_only_the_named_key() -> None:
    event_filter = EventFilter(date_from=dt.date(2026, 1, 1), limit=25, offset=25)
    query = filter_to_query(event_filter, page=4)
    restored = parse_event_filter(_params(query))

    assert restored.offset == 75
    assert restored.date_from == dt.date(2026, 1, 1)


def test_non_default_versions_survive_the_round_trip() -> None:
    query = "feature_version=earnings-reaction-v1&detector_version=other-v1"
    restored = parse_event_filter(_params(query))
    assert restored.versions.feature_version == "earnings-reaction-v1"
    assert restored.versions.detector_version == "other-v1"
    assert "feature_version=earnings-reaction-v1" in filter_to_query(restored)


def test_describe_filter_lists_only_active_refinements() -> None:
    described = dict(describe_filter(EventFilter(directions=("up",), min_volume_105m=500)))
    assert described == {"directions": "up", "min volume 105m": "500"}
    assert describe_filter(EventFilter(date_from=dt.date(2026, 1, 1))) == []

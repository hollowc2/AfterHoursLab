from __future__ import annotations

import datetime as dt

import pytest

from afterhours_lab.research import EventFilter, FeatureVersions
from afterhours_lab.research.filters import ORDER_BY, ParamBuilder, normalize_symbol


def test_symbols_are_normalized_and_deduplicated() -> None:
    event_filter = EventFilter(symbols=(" aapl ", "AAPL", "msft"))
    assert event_filter.symbols == ("AAPL", "MSFT")


def test_invalid_symbol_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid equity symbol"):
        EventFilter(symbols=("not a symbol",))


def test_unknown_enumeration_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown reaction_classes"):
        EventFilter(reaction_classes=("moon_shot",))


def test_unknown_sort_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown order_by"):
        EventFilter(order_by="; DROP TABLE earnings_events")


def test_every_sort_key_maps_to_a_vetted_fragment() -> None:
    for key in ORDER_BY:
        assert EventFilter(order_by=key).order_sql == ORDER_BY[key]


def test_reversed_date_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="date_from must be on or before date_to"):
        EventFilter(date_from=dt.date(2026, 5, 2), date_to=dt.date(2026, 5, 1))


def test_reversed_numeric_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_retention must not exceed max_retention"):
        EventFilter(min_retention=1.0, max_retention=0.5)


def test_coverage_ratio_must_be_a_proportion() -> None:
    with pytest.raises(ValueError, match="min_coverage_ratio"):
        EventFilter(min_coverage_ratio=1.5)


def test_limit_bounds_are_enforced() -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        EventFilter(limit=0)
    with pytest.raises(ValueError, match="limit must be between"):
        EventFilter(limit=10_000)


def test_blank_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="feature_version must be a non-empty string"):
        FeatureVersions(feature_version="  ")


def test_scope_sql_parameterizes_every_value() -> None:
    event_filter = EventFilter(
        date_from=dt.date(2026, 1, 1),
        date_to=dt.date(2026, 6, 30),
        symbols=("AAPL",),
    )
    params = ParamBuilder()
    sql = event_filter.scope_sql(params)

    assert sql == (
        "e.hour = 'amc' AND e.earnings_date >= $1 "
        "AND e.earnings_date <= $2 AND e.symbol = ANY($3::text[])"
    )
    assert params.params == [dt.date(2026, 1, 1), dt.date(2026, 6, 30), ["AAPL"]]


def test_refinement_sql_is_true_when_nothing_narrows_the_universe() -> None:
    params = ParamBuilder()
    assert EventFilter().refinement_sql(params) == "TRUE"
    assert params.params == []


def test_refinement_sql_carries_each_threshold_as_a_parameter() -> None:
    event_filter = EventFilter(
        reaction_classes=("spike_and_fade",),
        min_abs_initial_return=3.0,
        max_detection_delay_minutes=20,
        min_coverage_ratio=0.9,
    )
    params = ParamBuilder()
    sql = event_filter.refinement_sql(params)

    assert "reaction_class = ANY($1::text[])" in sql
    assert "abs(initial_return) >= $2" in sql
    assert "detection_delay_minutes <= $3" in sql
    assert "study_coverage_ratio >= $4" in sql
    assert params.params == [["spike_and_fade"], 3.0, 20, 0.9]


def test_param_builder_numbers_continue_across_clauses() -> None:
    event_filter = EventFilter(date_from=dt.date(2026, 1, 1), min_volume_105m=1000)
    params = ParamBuilder()
    event_filter.scope_sql(params)
    refinement = event_filter.refinement_sql(params)

    assert "volume_105m >= $2" in refinement
    assert params.params == [dt.date(2026, 1, 1), 1000]


def test_has_refinements_ignores_scope_and_paging() -> None:
    assert not EventFilter(
        date_from=dt.date(2026, 1, 1), symbols=("AAPL",), limit=10, offset=20
    ).has_refinements
    assert EventFilter(directions=("up",)).has_refinements


def test_normalize_symbol_uppercases_and_validates() -> None:
    assert normalize_symbol(" brk.b ") == "BRK.B"
    with pytest.raises(ValueError):
        normalize_symbol("")

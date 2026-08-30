"""The notebook helper is display logic plus a read-only pool; test both."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import EARNINGS_DATE, event_row

from afterhours_lab.research import EventFilter, FeatureVersions
from afterhours_lab.research.datasets import CohortResult, EventSummary
from afterhours_lab.research.notebook import (
    describe_cohort,
    describe_filter,
    development_split,
    research_pool,
    show_cohort,
)


def _cohort(**over) -> CohortResult:
    rows = tuple(EventSummary.from_row(event_row()) for _ in range(over.pop("n_rows", 2)))
    defaults = dict(
        rows=rows,
        universe_count=10,
        included_count=4,
        excluded_by_status=(("insufficient_data", 3), ("not_analyzed", 3)),
        event_filter=EventFilter(date_from=dt.date(2026, 1, 1), date_to=EARNINGS_DATE),
    )
    defaults.update(over)
    return CohortResult(**defaults)


def test_describe_filter_states_universe_refinements_and_versions():
    filt = EventFilter(
        date_from=dt.date(2026, 1, 1),
        date_to=dt.date(2026, 6, 30),
        symbols=("NVDA", "CRM"),
        reaction_classes=("spike_and_fade",),
        min_abs_initial_return=3.0,
    )
    text = describe_filter(filt)
    assert "2026-01-01" in text and "2026-06-30" in text
    assert "NVDA, CRM" in text
    assert "reaction classes=spike_and_fade" in text
    assert "min abs initial return=3.0" in text
    versions = FeatureVersions()
    assert versions.feature_version in text
    assert versions.detector_version in text
    assert versions.classifier_version in text


def test_describe_filter_reports_no_refinements_for_a_bare_universe():
    text = describe_filter(EventFilter(date_from=dt.date(2026, 1, 1)))
    assert "refinements:  (none)" in text
    assert "to open" in text  # open-ended date_to


def test_describe_cohort_discloses_counts_and_exclusion_breakdown():
    text = describe_cohort(_cohort())
    assert "universe:   10" in text
    assert "included:   4" in text
    assert "excluded:   6" in text
    assert "- insufficient_data: 3" in text
    assert "- not_analyzed: 3" in text


def test_describe_cohort_flags_a_truncated_page():
    text = describe_cohort(_cohort(n_rows=2, included_count=50))
    assert "cohort is larger" in text


def test_describe_cohort_does_not_flag_a_complete_page():
    text = describe_cohort(_cohort(n_rows=2, included_count=2, universe_count=2,
                                   excluded_by_status=()))
    assert "cohort is larger" not in text


def test_show_cohort_prints_and_returns_the_same_object(capsys):
    cohort = _cohort()
    returned = show_cohort(cohort)
    assert returned is cohort
    assert "included:   4" in capsys.readouterr().out


def test_development_split_is_ordered_and_non_overlapping():
    (dev_from, dev_to), (oos_from, oos_to) = development_split(
        dt.date(2026, 1, 1), dt.date(2026, 12, 31), holdout_fraction=0.25
    )
    assert dev_from == dt.date(2026, 1, 1)
    assert oos_to == dt.date(2026, 12, 31)
    assert dev_to < oos_from
    assert oos_from - dev_to == dt.timedelta(days=1)
    # ~25% of the ~364-day span held out
    assert 80 <= (oos_to - oos_from).days <= 95


@pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
def test_development_split_rejects_a_bad_holdout_fraction(fraction):
    with pytest.raises(ValueError):
        development_split(dt.date(2026, 1, 1), dt.date(2026, 12, 31), holdout_fraction=fraction)


def test_development_split_rejects_a_reversed_range():
    with pytest.raises(ValueError):
        development_split(dt.date(2026, 12, 31), dt.date(2026, 1, 1))


async def test_research_pool_opens_the_pool_read_only(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_connect(settings, *, read_only=False):
        seen["settings"] = settings
        seen["read_only"] = read_only
        return "pool-sentinel"

    monkeypatch.setattr(
        "afterhours_lab.research.notebook.DatabasePool.connect", fake_connect
    )

    class _Settings:
        pass

    result = await research_pool(_Settings())
    assert result == "pool-sentinel"
    assert seen["read_only"] is True

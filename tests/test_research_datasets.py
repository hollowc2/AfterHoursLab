from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    EARNINGS_DATE,
    POSTMARKET_START,
    RECEIVED_AT,
    FakeConnection,
    bar_row,
    coverage_row,
    event_row,
    note_row,
)

from afterhours_lab.research import (
    EventFilter,
    EventSummary,
    add_note,
    fetch_class_distribution,
    fetch_cohort,
    fetch_event_bars,
    fetch_event_detail,
    fetch_monitor_health,
    fetch_operations_snapshot,
    fetch_quality_issues,
    fetch_today,
    to_csv,
    to_records,
)
from afterhours_lab.research.datasets import CoveragePhase, _provisional_path


def test_summary_reports_missing_phases_and_study_readiness() -> None:
    summary = EventSummary.from_row(
        event_row(covered_phases=["earnings_regular", "following_regular"])
    )
    assert summary.missing_phases == ("earnings_postmarket", "following_premarket")
    assert not summary.study_ready

    complete = EventSummary.from_row(event_row())
    assert complete.missing_phases == ()
    assert complete.study_ready


def test_summary_flags_backfilled_capture() -> None:
    summary = EventSummary.from_row(
        event_row(earnings_postmarket_collection_mode="historical_backfill")
    )
    assert summary.any_historical_backfill


def test_summary_record_flattens_arrays_for_export() -> None:
    record = EventSummary.from_row(
        event_row(data_quality_flags=["stale", "gap"])
    ).to_record()
    assert record["data_quality_flags"] == "stale;gap"
    assert record["covered_phases"].startswith("earnings_postmarket;")


async def test_cohort_discloses_included_and_excluded_counts() -> None:
    conn = FakeConnection(
        events=[event_row()],
        universe_count=10,
        included_count=4,
        excluded_by_status={"insufficient_data": 5, "not_analyzed": 1},
    )
    cohort = await fetch_cohort(conn, EventFilter(reaction_classes=("immediate_continuation",)))

    assert cohort.universe_count == 10
    assert cohort.included_count == 4
    assert cohort.excluded_count == 6
    assert cohort.excluded_by_status == (("insufficient_data", 5), ("not_analyzed", 1))
    assert cohort.truncated  # one row shown of four included


async def test_cohort_query_binds_the_requested_feature_versions() -> None:
    conn = FakeConnection()
    await fetch_cohort(conn, EventFilter())

    universe_sql = conn.sql_seen[0]
    assert "f.feature_version = $1" in universe_sql
    assert "f.detector_version = $2" in universe_sql
    assert "f.classifier_version = $3" in universe_sql


async def test_event_bars_read_only_the_retrieval_named_by_coverage() -> None:
    coverage = (CoveragePhase.from_row(coverage_row("earnings_postmarket")),)
    conn = FakeConnection(phase_bars=[bar_row(0), bar_row(1, 104.0)])

    bars = await fetch_event_bars(conn, "TEST", EARNINGS_DATE, coverage)

    assert [bar.phase for bar in bars] == ["earnings_postmarket"] * 2
    sql = conn.sql_seen[0]
    assert "gateway_received_at = $4" in sql
    assert "source = $5" in sql


async def test_event_detail_assembles_summary_coverage_bars_and_notes() -> None:
    conn = FakeConnection(
        events=[event_row()],
        coverage=[coverage_row("earnings_regular"), coverage_row("earnings_postmarket")],
        phase_bars=[bar_row(0)],
        notes=[note_row()],
    )
    detail = await fetch_event_detail(conn, "test", EARNINGS_DATE)

    assert detail is not None
    assert detail.summary.symbol == "TEST"
    assert len(detail.coverage) == 2
    assert detail.notes[0].author == "researcher"
    assert detail.parameters == {"threshold_pct": 2.0}
    assert detail.postmarket_start == POSTMARKET_START
    assert detail.study_cutoff == POSTMARKET_START + dt.timedelta(minutes=105)


async def test_event_detail_is_none_for_an_unknown_event() -> None:
    conn = FakeConnection(events=[])
    assert await fetch_event_detail(conn, "TEST", EARNINGS_DATE) is None


async def test_add_note_rejects_blank_input_and_normalizes_tags() -> None:
    conn = FakeConnection()
    with pytest.raises(ValueError, match="note body must not be blank"):
        await add_note(conn, "TEST", EARNINGS_DATE, author="a", body="   ")
    with pytest.raises(ValueError, match="author must not be blank"):
        await add_note(conn, "TEST", EARNINGS_DATE, author=" ", body="text")

    note = await add_note(
        conn,
        "test",
        EARNINGS_DATE,
        author="researcher",
        body="  faded into the close  ",
        tags=("Fade", " fade ", "", "Thin-Book"),
    )
    assert note.symbol == "TEST"
    assert note.body == "faded into the close"
    assert note.tags == ("fade", "thin-book")


def test_provisional_path_uses_close_confirmed_signal_timing() -> None:
    bars = [
        (POSTMARKET_START, 100.5),
        (POSTMARKET_START + dt.timedelta(minutes=1), 103.0),
        (POSTMARKET_START + dt.timedelta(minutes=2), 104.0),
    ]
    move, cross_ts, cross_pct = _provisional_path(100.0, bars)

    assert move == pytest.approx(4.0)
    # The 16:01 bar crossed, so the signal is knowable at the end of that minute.
    assert cross_ts == POSTMARKET_START + dt.timedelta(minutes=2)
    assert cross_pct == pytest.approx(3.0)


def test_provisional_path_without_a_reference_returns_nothing() -> None:
    assert _provisional_path(None, [(POSTMARKET_START, 100.0)]) == (None, None, None)
    assert _provisional_path(100.0, []) == (None, None, None)


async def test_today_marks_unfinalized_rows_and_lists_degradation_reasons() -> None:
    conn = FakeConnection(
        events=[event_row(analysis_status=None, reaction_class=None)],
        quotes=[
            {
                "symbol": "TEST",
                "bid": 103.0,
                "ask": 103.4,
                "mark": 103.2,
                "last": 103.1,
                "volume": 5_000,
                "gateway_received_at": RECEIVED_AT,
                "stale": True,
                "age_seconds": 12.0,
                "data_quality_flags": ["stale", "wide_spread"],
            }
        ],
        pre_close={},
        postmarket=[],
    )
    conn.pre_close = [{"symbol": "TEST", "ts": POSTMARKET_START, "close": 100.0}]
    conn.postmarket = [
        {"symbol": "TEST", "ts": POSTMARKET_START, "close": 103.0},
    ]

    [candidate] = await fetch_today(conn, EARNINGS_DATE)

    assert not candidate.finalized
    assert candidate.spread == pytest.approx(0.4)
    assert candidate.provisional_move_pct == pytest.approx(3.0)
    assert candidate.first_threshold_cross_ts == POSTMARKET_START + dt.timedelta(minutes=1)
    assert "gateway reported the quote as stale" in candidate.degradation_reasons
    assert "wide_spread" in candidate.degradation_reasons
    assert "no finalized feature row yet" in candidate.degradation_reasons
    # the raw "stale" flag is dropped when quote_stale already reported it
    assert "stale" not in candidate.degradation_reasons
    assert len(candidate.degradation_reasons) == len(set(candidate.degradation_reasons))


async def test_today_without_quote_evidence_says_so() -> None:
    conn = FakeConnection(events=[event_row()], quotes=[], pre_close=[], postmarket=[])
    [candidate] = await fetch_today(conn, EARNINGS_DATE)

    assert candidate.bid is None
    assert "no quote evidence recorded for this event" in candidate.degradation_reasons
    assert "no pre-close reference minute recorded" in candidate.degradation_reasons


async def test_operations_snapshot_reads_each_writer_s_own_watermark() -> None:
    conn = FakeConnection()
    snapshot = await fetch_operations_snapshot(conn)

    assert snapshot.last_bar_received_at == RECEIVED_AT
    assert snapshot.backfill_coverage_rows == 2
    assert snapshot.feature_rows == 7


async def test_monitor_health_maps_durable_operational_state() -> None:
    conn = FakeConnection()
    health = await fetch_monitor_health(conn, EARNINGS_DATE)

    assert health.market_date == EARNINGS_DATE
    assert health.status == "success"
    assert health.active_window is True
    assert health.candidate_count == 1
    assert health.last_inserted_quote_count == 1
    assert health.freshest_quote_at == RECEIVED_AT


async def test_quality_issues_name_the_specific_defect() -> None:
    conn = FakeConnection(
        events=[
            event_row(symbol="MISS", covered_phases=["following_regular"]),
            event_row(symbol="NONE", analysis_status=None, reaction_class=None),
            event_row(
                symbol="THIN",
                analysis_status="insufficient_data",
                reaction_class=None,
                analysis_status_reason="missing exact checkpoint bar(s): 60m",
                missing_fields=["return_60m"],
            ),
            event_row(symbol="BACK", earnings_postmarket_collection_mode="historical_backfill"),
            event_row(symbol="FLAG", data_quality_flags=["duplicate_minute_bars"]),
            event_row(symbol="GOOD"),
        ]
    )
    issues = await fetch_quality_issues(conn, EventFilter())
    by_symbol = {issue.symbol: issue for issue in issues}

    assert by_symbol["MISS"].kind == "missing_capture_phase"
    assert by_symbol["NONE"].kind == "not_analyzed"
    assert by_symbol["THIN"].kind == "insufficient_data"
    assert by_symbol["BACK"].kind == "historical_backfill"
    assert by_symbol["FLAG"].kind == "data_quality_flag"
    assert "GOOD" not in by_symbol


async def test_class_distribution_groups_unanalyzed_events() -> None:
    conn = FakeConnection(
        distribution=[
            {"reaction_class": "spike_and_fade", "total": 3},
            {"reaction_class": "not_analyzed", "total": 1},
        ]
    )
    assert await fetch_class_distribution(conn, EventFilter()) == (
        ("spike_and_fade", 3),
        ("not_analyzed", 1),
    )


def test_csv_export_has_a_stable_header_and_flattened_arrays() -> None:
    rows = [EventSummary.from_row(event_row(data_quality_flags=["stale"]))]
    csv_text = to_csv(rows)
    header, first = csv_text.splitlines()[:2]

    assert header.split(",")[:2] == ["symbol", "earnings_date"]
    assert "stale" in first
    assert to_csv([]) == ""
    assert to_records(rows)[0]["symbol"] == "TEST"

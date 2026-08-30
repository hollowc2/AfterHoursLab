from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from afterhours_lab.persist_reactions import (
    INSERT_COLUMNS,
    INSERT_SQL,
    NUMERIC_FIELDS,
    build_row,
    evidence_digest,
    persist_features,
)
from afterhours_lab.reactions import (
    Bar,
    EventEvidence,
    PathClassification,
    compute_reaction_features,
)

EASTERN = ZoneInfo("America/New_York")
DATE = dt.date(2026, 8, 20)
START = dt.datetime.combine(DATE, dt.time(16), EASTERN)


def _event(close_at: Callable[[int], float], **overrides: Any) -> EventEvidence:
    postmarket = tuple(
        Bar(
            ts=START + dt.timedelta(minutes=minute),
            open=close_at(minute),
            high=close_at(minute) + 0.1,
            low=close_at(minute) - 0.1,
            close=close_at(minute),
            volume=10,
        )
        for minute in range(105)
    )
    event = EventEvidence(
        symbol="TEST",
        earnings_date=DATE,
        regular_covered=True,
        postmarket_covered=True,
        postmarket_expected_start=START,
        postmarket_expected_end=START + dt.timedelta(hours=4),
        regular_expected_start=START - dt.timedelta(hours=6, minutes=30),
        regular_expected_end=START,
        regular_bars=(
            Bar(
                ts=START - dt.timedelta(minutes=1),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=10,
            ),
        ),
        postmarket_bars=postmarket,
        regular_collection_mode="scheduled_capture",
        postmarket_collection_mode="scheduled_capture",
        regular_response_sha256="a" * 64,
        postmarket_response_sha256="b" * 64,
    )
    return dataclasses.replace(event, **overrides) if overrides else event


def _row_for(close_at: Callable[[int], float], **overrides: Any) -> dict[str, Any]:
    event = _event(close_at, **overrides)
    return build_row(event, compute_reaction_features(event))


# --------------------------------------------------------------- insert contract


def test_insert_statement_and_column_list_agree() -> None:
    placeholders = {int(match) for match in re.findall(r"\$(\d+)", INSERT_SQL)}
    assert placeholders == set(range(1, len(INSERT_COLUMNS) + 1))


def test_insert_is_append_only_on_the_version_triple() -> None:
    assert "ON CONFLICT (symbol, earnings_date, feature_version" in INSERT_SQL
    assert "DO NOTHING" in INSERT_SQL
    assert "DO UPDATE" not in INSERT_SQL


# ------------------------------------------------------------------- row mapping


def test_complete_row_populates_every_numeric_field() -> None:
    row = _row_for(lambda minute: 100.0 + 3.0 * (minute + 1) / 105.0 + (3.0 if minute else 0))

    assert row["analysis_status"] == "complete"
    assert row["reaction_class"] in {
        "immediate_continuation",
        "spike_and_fade",
        "delayed_breakout",
        "whipsaw",
    }
    assert row["reaction_direction"] == "up"
    assert row["analysis_status_reason"] is None
    assert row["missing_fields"] == []
    # The table's CHECK for a complete row requires all twenty to be present.
    assert [name for name in NUMERIC_FIELDS if row[name] is None] == []
    assert row["feature_window_start"] == START
    assert row["feature_cutoff_ts"] == START + dt.timedelta(minutes=105)
    assert row["label_cutoff_ts"] == row["feature_cutoff_ts"]
    assert row["feature_window_start"] <= row["reaction_timestamp"] <= row["feature_cutoff_ts"]


def test_downward_reaction_is_recorded_as_a_down_direction() -> None:
    row = _row_for(lambda minute: 100.0 - (3.0 if minute else 0.0))

    assert row["analysis_status"] == "complete"
    assert row["reaction_direction"] == "down"
    assert row["initial_return"] < 0


def test_no_trigger_row_leaves_exactly_the_post_signal_fields_null() -> None:
    row = _row_for(lambda minute: 100.0 + 0.1)

    assert row["analysis_status"] == "no_trigger_in_window"
    assert row["reaction_class"] == "no_trigger_in_window"
    assert row["reaction_direction"] == "flat"
    assert row["analysis_status_reason"] is not None
    assert row["missing_fields"] == []
    # The table's CHECK counts exactly sixteen non-null numeric fields here.
    present = [name for name in NUMERIC_FIELDS if row[name] is not None]
    assert len(present) == 16
    assert row["initial_return"] is None
    assert row["max_favorable_excursion"] is None
    assert row["max_adverse_excursion"] is None
    assert row["max_retracement"] is None
    assert row["reference_price"] == 100.0
    assert row["window_high_return"] is not None


def test_insufficient_data_row_lists_its_missing_fields() -> None:
    event = _event(lambda minute: 100.0, postmarket_covered=False)
    row = build_row(event, compute_reaction_features(event))

    assert row["analysis_status"] == "insufficient_data"
    assert row["reaction_class"] is None
    assert row["reaction_direction"] is None
    assert row["analysis_status_reason"]
    assert set(row["missing_fields"]) == set(NUMERIC_FIELDS)


def test_window_columns_are_omitted_when_the_phase_is_too_short() -> None:
    """An early close cannot satisfy label_cutoff_ts <= feature_window_end."""
    event = _event(
        lambda minute: 100.0,
        postmarket_expected_end=START + dt.timedelta(minutes=30),
    )
    row = build_row(event, compute_reaction_features(event))

    assert row["analysis_status"] == "insufficient_data"
    assert row["feature_window_start"] is None
    assert row["feature_cutoff_ts"] is None
    assert row["label_cutoff_ts"] is None


def test_row_carries_the_algorithm_definition_and_capture_provenance() -> None:
    row = _row_for(lambda minute: 100.0 + (3.0 if minute else 0.0))
    parameters = json.loads(row["parameters"])

    assert parameters["reaction_threshold_pct"] == 2.0
    assert parameters["horizons_minutes"] == [1, 5, 15, 30, 60, 105]
    assert parameters["study_window_minutes"] == 105
    assert row["earnings_regular_response_sha256"] == "a" * 64
    assert row["earnings_postmarket_response_sha256"] == "b" * 64
    assert row["earnings_postmarket_collection_mode"] == "scheduled_capture"
    assert row["source_bar_count"] == 106


def test_response_hash_and_collection_mode_are_null_together() -> None:
    """The table pairs each hash with its mode; one without the other violates a CHECK."""
    event = _event(
        lambda minute: 100.0,
        regular_covered=False,
        regular_response_sha256=None,
        regular_collection_mode=None,
    )
    row = build_row(event, compute_reaction_features(event))

    assert row["earnings_regular_response_sha256"] is None
    assert row["earnings_regular_collection_mode"] is None


# ---------------------------------------------------------------------- digest


def test_evidence_digest_is_stable_for_identical_evidence() -> None:
    assert evidence_digest(_event(lambda m: 100.0)) == evidence_digest(_event(lambda m: 100.0))
    assert re.fullmatch(r"[0-9a-f]{64}", evidence_digest(_event(lambda m: 100.0)))


def test_evidence_digest_changes_when_a_bar_changes() -> None:
    baseline = _event(lambda minute: 100.0)
    altered = dataclasses.replace(
        baseline,
        postmarket_bars=baseline.postmarket_bars[:-1]
        + (dataclasses.replace(baseline.postmarket_bars[-1], close=101.0),),
    )
    assert evidence_digest(baseline) != evidence_digest(altered)


def test_evidence_digest_changes_when_the_chosen_retrieval_changes() -> None:
    baseline = _event(lambda minute: 100.0)
    altered = dataclasses.replace(baseline, postmarket_response_sha256="d" * 64)
    assert evidence_digest(baseline) != evidence_digest(altered)


# --------------------------------------------------------------------- writing


class RecordingConnection:
    """Accepts the first insert per key and reports the rest as already present."""

    def __init__(self) -> None:
        self.seen: set[tuple[Any, ...]] = set()
        self.calls: list[list[Any]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(list(args))
        key = tuple(args[:6])
        if key in self.seen:
            return None
        self.seen.add(key)
        return args[0]


async def test_persist_inserts_once_and_reports_repeats_as_already_present() -> None:
    conn = RecordingConnection()
    events = [_event(lambda minute: 100.0 + (3.0 if minute else 0.0))]

    first = await persist_features(conn, events)
    second = await persist_features(conn, events)

    assert (first.inserted, first.already_present) == (1, 0)
    assert (second.inserted, second.already_present) == (0, 1)
    assert second.considered == 1


async def test_persist_dry_run_writes_nothing() -> None:
    conn = RecordingConnection()
    outcome = await persist_features(
        conn, [_event(lambda minute: 100.0)], dry_run=True
    )

    assert conn.calls == []
    assert outcome.inserted == 0
    assert len(outcome.rows) == 1


async def test_persist_sends_values_in_the_declared_column_order() -> None:
    conn = RecordingConnection()
    events = [_event(lambda minute: 100.0 + (3.0 if minute else 0.0))]
    outcome = await persist_features(conn, events)

    [values] = conn.calls
    assert len(values) == len(INSERT_COLUMNS)
    for index, column in enumerate(INSERT_COLUMNS):
        assert values[index] == outcome.rows[0][column]


def test_classification_to_status_mapping_covers_every_classification() -> None:
    from afterhours_lab.persist_reactions import STATUS_BY_CLASSIFICATION

    assert set(STATUS_BY_CLASSIFICATION) == set(PathClassification)


@pytest.mark.parametrize("column", INSERT_COLUMNS)
def test_every_insert_column_is_produced_by_build_row(column: str) -> None:
    row = _row_for(lambda minute: 100.0 + (3.0 if minute else 0.0))
    assert column in row

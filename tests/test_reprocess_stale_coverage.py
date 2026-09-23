from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from zoneinfo import ZoneInfo

from afterhours_lab import reprocess_stale_coverage as rsc
from afterhours_lab.persist_reactions import build_row, evidence_digest
from afterhours_lab.reactions import Bar, EventEvidence, compute_reaction_features

EASTERN = ZoneInfo("America/New_York")
DATE = dt.date(2026, 8, 25)
START = dt.datetime.combine(DATE, dt.time(16), EASTERN)


def _covered_event(close_at: Callable[[int], float], **overrides: Any) -> EventEvidence:
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
        symbol="ZM",
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
        regular_collection_mode="historical_backfill",
        postmarket_collection_mode="historical_backfill",
        regular_response_sha256="a" * 64,
        postmarket_response_sha256="b" * 64,
    )
    return dataclasses.replace(event, **overrides) if overrides else event


def _no_coverage_event(**overrides: Any) -> EventEvidence:
    """What the original stale row was computed from: no coverage at all."""
    event = EventEvidence(
        symbol="ZM",
        earnings_date=DATE,
        regular_covered=False,
        postmarket_covered=False,
        postmarket_expected_start=None,
        postmarket_expected_end=None,
        regular_expected_start=None,
        regular_expected_end=None,
        regular_bars=(),
        postmarket_bars=(),
    )
    return dataclasses.replace(event, **overrides) if overrides else event


class RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        return args[0]

    @asynccontextmanager
    async def transaction(self):
        yield


async def test_reprocess_retracts_and_reinserts_when_evidence_changed() -> None:
    stale_digest = evidence_digest(_no_coverage_event())
    stale = rsc.StaleRow(symbol="ZM", earnings_date=DATE, source_evidence_sha256=stale_digest)
    covered_event = _covered_event(lambda minute: 100.0 + (3.0 if minute else 0.0))
    conn = RecordingConnection()

    outcome = await rsc.reprocess_stale_coverage(
        conn, [stale], {("ZM", DATE): covered_event}
    )

    assert [(s, d) for s, d, _ in outcome.reprocessed] == [("ZM", DATE)]
    assert outcome.unchanged == ()

    [(update_sql, update_args)] = conn.executed
    assert "UPDATE earnings_reaction_features" in update_sql
    assert "retracted_at = now()" in update_sql
    assert update_args[0] == "ZM"
    assert update_args[1] == DATE

    [(insert_sql, insert_args)] = conn.fetchval_calls
    assert "INSERT INTO earnings_reaction_features" in insert_sql
    expected_row = build_row(covered_event, compute_reaction_features(covered_event))
    assert insert_args[0] == expected_row["symbol"]
    assert insert_args[6] == expected_row["analysis_status"]


async def test_reprocess_leaves_row_alone_when_evidence_is_unchanged() -> None:
    """No new coverage arrived: the digest matches, so nothing is written."""
    no_coverage = _no_coverage_event()
    stale_digest = evidence_digest(no_coverage)
    stale = rsc.StaleRow(symbol="ZM", earnings_date=DATE, source_evidence_sha256=stale_digest)
    conn = RecordingConnection()

    outcome = await rsc.reprocess_stale_coverage(
        conn, [stale], {("ZM", DATE): no_coverage}
    )

    assert outcome.reprocessed == ()
    assert outcome.unchanged == (("ZM", DATE),)
    assert conn.executed == []
    assert conn.fetchval_calls == []


async def test_dry_run_identifies_candidates_without_writing() -> None:
    stale_digest = evidence_digest(_no_coverage_event())
    stale = rsc.StaleRow(symbol="ZM", earnings_date=DATE, source_evidence_sha256=stale_digest)
    covered_event = _covered_event(lambda minute: 100.0)
    conn = RecordingConnection()

    outcome = await rsc.reprocess_stale_coverage(
        conn, [stale], {("ZM", DATE): covered_event}, dry_run=True
    )

    assert [(s, d) for s, d, _ in outcome.reprocessed] == [("ZM", DATE)]
    assert conn.executed == []
    assert conn.fetchval_calls == []


async def test_reprocess_skips_a_stale_row_with_no_matching_event() -> None:
    stale = rsc.StaleRow(symbol="MISSING", earnings_date=DATE, source_evidence_sha256="d" * 64)
    conn = RecordingConnection()

    outcome = await rsc.reprocess_stale_coverage(conn, [stale], {})

    assert outcome.reprocessed == ()
    assert outcome.unchanged == ()
    assert conn.executed == []


class FakeQueryConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql_seen: list[str] = []
        self.args_seen: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql_seen.append(sql)
        self.args_seen.append(args)
        return self._rows


async def test_fetch_stale_missing_coverage_rows_filters_on_reason_and_live_rows() -> None:
    rows = [{"symbol": "ZM", "earnings_date": DATE, "source_evidence_sha256": "c" * 64}]
    conn = FakeQueryConnection(rows)

    stale_rows = await rsc.fetch_stale_missing_coverage_rows(conn)

    assert stale_rows == [
        rsc.StaleRow(symbol="ZM", earnings_date=DATE, source_evidence_sha256="c" * 64)
    ]
    [sql] = conn.sql_seen
    assert "retracted_at IS NULL" in sql
    assert "analysis_status = 'insufficient_data'" in sql
    assert "analysis_status_reason = $4" in sql
    [args] = conn.args_seen
    assert args[-1] == rsc.STALE_REASON

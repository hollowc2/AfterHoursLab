from __future__ import annotations

import dataclasses
import datetime as dt
from contextlib import asynccontextmanager

import pytest

from afterhours_lab.outcomes import (
    FollowingSessionEvidence,
    OutcomeBar,
    OutcomeCoverage,
    compute_following_session_outcome,
    source_evidence_digest,
)
from afterhours_lab.persist_outcomes import INSERT_COLUMNS, INSERT_SQL, persist_outcomes

UTC = dt.UTC
DATE = dt.date(2026, 7, 2)
REGULAR_OPEN = dt.datetime(2026, 7, 6, 13, 30, tzinfo=UTC)


def _bar(ts: dt.datetime, close: float) -> OutcomeBar:
    return OutcomeBar(ts=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


def _coverage(phase: str, start: dt.datetime, bars: tuple[OutcomeBar, ...]) -> OutcomeCoverage:
    expected_end = max(bar.ts for bar in bars) + dt.timedelta(minutes=1)
    return OutcomeCoverage(
        phase=phase,
        market_date=start.date(),
        session="regular" if "premarket" not in phase else "extended",
        expected_start=start,
        expected_end=expected_end,
        observed_minutes=len(bars),
        expected_minutes=len(bars),
        source="schwab",
        gateway_received_at=start + dt.timedelta(hours=8),
        response_sha256={
            "earnings_regular": "a",
            "following_premarket": "b",
            "following_regular": "c",
        }[phase]
        * 64,
        collection_mode="scheduled_capture",
        calendar="XNYS",
        calendar_version="4",
        data_quality_flags=(),
        bars=bars,
    )


def _evidence() -> FollowingSessionEvidence:
    reference_start = dt.datetime(2026, 7, 2, 13, 30, tzinfo=UTC)
    premarket_start = dt.datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
    following = tuple(
        _bar(REGULAR_OPEN + dt.timedelta(minutes=minute), 110 + minute) for minute in range(390)
    )
    return FollowingSessionEvidence(
        symbol="TEST",
        earnings_date=DATE,
        expected_following_close=REGULAR_OPEN + dt.timedelta(minutes=390),
        regular=_coverage(
            "earnings_regular",
            reference_start,
            (_bar(reference_start, 99), _bar(reference_start + dt.timedelta(minutes=389), 100)),
        ),
        premarket=_coverage(
            "following_premarket",
            premarket_start,
            (_bar(premarket_start, 105), _bar(premarket_start + dt.timedelta(minutes=1), 106)),
        ),
        following_regular=_coverage("following_regular", REGULAR_OPEN, following),
    )


def test_exact_close_confirmed_horizon_semantics_and_holiday_boundary() -> None:
    evidence = _evidence()  # July 2 event closes on Monday July 6, after weekend/July 3 holiday.
    result = compute_following_session_outcome(
        evidence, now=evidence.expected_following_close + dt.timedelta(minutes=1)
    )

    assert result["analysis_status"] == "complete"
    assert result["reference_close"] == 100
    assert result["regular_return_5m"] == pytest.approx(14.0)  # open+4m closes minute five
    assert result["regular_return_30m"] == pytest.approx(39.0)
    assert result["regular_return_60m"] == pytest.approx(69.0)


def test_missing_exact_horizon_is_not_replaced_by_a_neighbor() -> None:
    evidence = _evidence()
    coverage = evidence.following_regular
    assert coverage is not None
    bars = tuple(bar for bar in coverage.bars if bar.ts != REGULAR_OPEN + dt.timedelta(minutes=4))
    evidence = dataclasses.replace(
        evidence, following_regular=dataclasses.replace(coverage, bars=bars)
    )
    result = compute_following_session_outcome(
        evidence, now=evidence.expected_following_close + dt.timedelta(minutes=1)
    )

    assert result["analysis_status"] == "insufficient_data"
    assert result["regular_return_5m"] is None
    assert "regular_return_5m" in result["missing_fields"]


def test_not_yet_available_is_distinct_from_bad_coverage() -> None:
    evidence = dataclasses.replace(_evidence(), following_regular=None)
    before_close = compute_following_session_outcome(
        evidence, now=evidence.expected_following_close - dt.timedelta(minutes=1)
    )
    after_close = compute_following_session_outcome(
        evidence, now=evidence.expected_following_close + dt.timedelta(minutes=11)
    )
    assert before_close["analysis_status"] == "not_yet_available"
    assert after_close["analysis_status"] == "insufficient_data"


def test_missing_capture_remains_not_yet_available_during_capture_grace() -> None:
    evidence = dataclasses.replace(_evidence(), following_regular=None)
    result = compute_following_session_outcome(
        evidence, now=evidence.expected_following_close + dt.timedelta(minutes=5)
    )
    assert result["analysis_status"] == "not_yet_available"
    assert result["parameters"]["authoritative_capture_grace_minutes"] == 10


def test_source_digest_is_order_independent_but_evidence_sensitive() -> None:
    evidence = _evidence()
    coverage = evidence.following_regular
    assert coverage is not None
    reordered = dataclasses.replace(
        evidence,
        following_regular=dataclasses.replace(coverage, bars=tuple(reversed(coverage.bars))),
    )
    assert source_evidence_digest(reordered) == source_evidence_digest(evidence)
    changed_bars = list(coverage.bars)
    changed_bars[0] = dataclasses.replace(changed_bars[0], close=999)
    changed = dataclasses.replace(
        evidence, following_regular=dataclasses.replace(coverage, bars=tuple(changed_bars))
    )
    assert source_evidence_digest(changed) != source_evidence_digest(evidence)


class FakeConnection:
    def __init__(self, results: list[str | None]) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.transactions = 0

    async def fetch(self, sql: str, *values: object) -> list[dict[str, str]]:
        self.calls.append((sql, values))
        result = self.results.pop(0)
        return [{"symbol": result}] if result is not None else []

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield


@pytest.mark.asyncio
async def test_outcome_persistence_is_idempotent_and_dry_run_writes_nothing() -> None:
    def clock() -> dt.datetime:
        return _evidence().expected_following_close + dt.timedelta(minutes=1)
    dry_conn = FakeConnection([])
    dry = await persist_outcomes(dry_conn, [_evidence()], dry_run=True, clock=clock)
    assert dry.scanned == 1 and dry.complete == 1
    assert dry.inserted == 0 and dry_conn.calls == []

    conn = FakeConnection(["TEST", None])
    first = await persist_outcomes(conn, [_evidence()], clock=clock)
    second = await persist_outcomes(conn, [_evidence()], clock=clock)
    assert first.inserted == 1 and first.conflicted == 0
    assert second.inserted == 0 and second.conflicted == 1
    assert all(len(values) == len(INSERT_COLUMNS) for _, values in conn.calls)
    assert "DO NOTHING" in INSERT_SQL and "DO UPDATE" not in INSERT_SQL
    assert conn.transactions == 2


@pytest.mark.asyncio
async def test_transient_not_yet_available_is_never_persisted() -> None:
    evidence = dataclasses.replace(_evidence(), following_regular=None)
    conn = FakeConnection([])
    summary = await persist_outcomes(
        conn,
        [evidence],
        clock=lambda: evidence.expected_following_close - dt.timedelta(minutes=1),
    )
    assert summary.not_yet_available == 1
    assert summary.inserted == summary.conflicted == 0
    assert conn.calls == []

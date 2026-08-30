"""Shared fakes for the research layer and the website.

The research layer's contract is SQL text plus row shapes, so a fake connection that
dispatches on recognisable fragments of that SQL exercises the real query builders,
the real dataclass mapping, and the real templates without a live database.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

EARNINGS_DATE = dt.date(2026, 8, 20)
POSTMARKET_START = dt.datetime.combine(EARNINGS_DATE, dt.time(16), EASTERN)
REGULAR_START = dt.datetime.combine(EARNINGS_DATE, dt.time(9, 30), EASTERN)
RECEIVED_AT = POSTMARKET_START + dt.timedelta(hours=4, minutes=5)


def event_row(**overrides: Any) -> dict[str, Any]:
    """One row of the universe CTE, in the shape ``EventSummary.from_row`` expects."""
    row: dict[str, Any] = {
        "symbol": "TEST",
        "earnings_date": EARNINGS_DATE,
        "hour": "amc",
        "eps_estimate": 1.00,
        "eps_actual": 1.25,
        "revenue_estimate": 1_000_000.0,
        "revenue_actual": 1_100_000.0,
        "quarter": 3,
        "year": 2026,
        "eps_surprise_pct": 25.0,
        "revenue_surprise_pct": 10.0,
        "feature_version": "earnings-reaction-v1",
        "detector_version": "fixed-preclose-2pct-v1",
        "classifier_version": "path-retention-v1",
        "analysis_status": "complete",
        "analysis_status_reason": None,
        "reaction_class": "immediate_continuation",
        "reaction_direction": "up",
        "reaction_timestamp": POSTMARKET_START + dt.timedelta(minutes=3),
        "detection_delay_minutes": 3,
        "reference_price": 100.0,
        "initial_return": 4.0,
        "return_1m": 1.0,
        "return_5m": 3.5,
        "return_15m": 4.2,
        "return_30m": 4.4,
        "return_60m": 4.6,
        "return_105m": 5.0,
        "retention": 1.25,
        "window_high_return": 5.5,
        "window_low_return": -0.5,
        "max_favorable_excursion": 5.5,
        "max_adverse_excursion": -0.2,
        "max_retracement": 9.0,
        "reaction_vwap_proxy": 103.5,
        "volume_105m": 250_000,
        "study_observed_minutes": 105,
        "study_missing_minutes": 0,
        "study_coverage_ratio": 1.0,
        "earnings_regular_collection_mode": "scheduled_capture",
        "earnings_postmarket_collection_mode": "scheduled_capture",
        "source_evidence_sha256": "c" * 64,
        "data_quality_flags": [],
        "missing_fields": [],
        "computed_at": RECEIVED_AT,
        "covered_phases": [
            "earnings_postmarket",
            "earnings_regular",
            "following_premarket",
            "following_regular",
        ],
        "note_count": 1,
    }
    row.update(overrides)
    return row


def coverage_row(phase: str = "earnings_postmarket", **overrides: Any) -> dict[str, Any]:
    session = "regular" if phase.endswith("regular") else "extended"
    start = POSTMARKET_START if phase == "earnings_postmarket" else REGULAR_START
    end = (
        POSTMARKET_START + dt.timedelta(hours=4)
        if phase == "earnings_postmarket"
        else POSTMARKET_START
    )
    row: dict[str, Any] = {
        "symbol": "TEST",
        "earnings_date": EARNINGS_DATE,
        "phase": phase,
        "market_date": EARNINGS_DATE,
        "session": session,
        "expected_start": start,
        "expected_end": end,
        "observed_first": start,
        "observed_last": end - dt.timedelta(minutes=1),
        "observed_minutes": 105,
        "expected_minutes": 240,
        "source": "schwab",
        "gateway_received_at": RECEIVED_AT,
        "response_sha256": "a" * 64,
        "data_quality_flags": [],
        "collection_mode": "scheduled_capture",
        "calendar": "XNYS",
        "calendar_version": "4.13.2",
        "retrieved_at": RECEIVED_AT,
    }
    row.update(overrides)
    return row


def bar_row(minute: int, close: float = 100.0, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": POSTMARKET_START + dt.timedelta(minutes=minute),
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": 1_000,
    }
    row.update(overrides)
    return row


def note_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "symbol": "TEST",
        "earnings_date": EARNINGS_DATE,
        "author": "researcher",
        "body": "Thin book above the trigger.",
        "tags": ["liquidity"],
        "created_at": RECEIVED_AT,
    }
    row.update(overrides)
    return row


def operations_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "last_event": RECEIVED_AT,
        "last_coverage": RECEIVED_AT,
        "last_bar": RECEIVED_AT,
        "last_quote": RECEIVED_AT,
        "last_feature": RECEIVED_AT,
        "events_total": 12,
        "events_amc": 9,
        "coverage_rows": 30,
        "scheduled_rows": 28,
        "backfill_rows": 2,
        "feature_rows": 7,
    }
    row.update(overrides)
    return row


class FakeConnection:
    """Dispatches on SQL fragments the research layer actually emits."""

    def __init__(self, **canned: Any) -> None:
        self.events: list[dict[str, Any]] = list(canned.get("events", [event_row()]))
        self.universe_count: int = canned.get("universe_count", len(self.events))
        self.included_count: int = canned.get("included_count", len(self.events))
        self.excluded_by_status: dict[str, int] = canned.get("excluded_by_status", {})
        self.coverage: list[dict[str, Any]] = list(canned.get("coverage", []))
        self.phase_bars: list[dict[str, Any]] = list(canned.get("phase_bars", []))
        self.notes: list[dict[str, Any]] = list(canned.get("notes", []))
        self.quotes: list[dict[str, Any]] = list(canned.get("quotes", []))
        self.pre_close: list[dict[str, Any]] = list(canned.get("pre_close", []))
        self.postmarket: list[dict[str, Any]] = list(canned.get("postmarket", []))
        self.distribution: list[dict[str, Any]] = list(canned.get("distribution", []))
        self.monthly: list[dict[str, Any]] = list(canned.get("monthly", []))
        self.operations: dict[str, Any] = canned.get("operations", operations_row())
        self.parameters: Any = canned.get("parameters", json.dumps({"threshold_pct": 2.0}))
        self.sql_seen: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql_seen.append(sql)
        if "gateway_endpoint = '/v1/session-history'" in sql:
            return self.phase_bars
        if "FROM bar_evidence" in sql and "session = 'regular'" in sql:
            return self.pre_close
        if "FROM bar_evidence" in sql and "session = 'extended'" in sql:
            return self.postmarket
        if "FROM quote_evidence" in sql:
            return self.quotes
        if "SELECT * FROM earnings_ohlcv_coverage" in sql:
            return self.coverage
        if "SELECT id, symbol, earnings_date, author" in sql:
            return self.notes
        if "COALESCE(reaction_class, 'not_analyzed')" in sql:
            return self.distribution
        if "to_char(date_trunc" in sql:
            return self.monthly
        return self.events

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.sql_seen.append(sql)
        if "universe_count" in sql:
            return {
                "universe_count": self.universe_count,
                "included_count": self.included_count,
                "excluded_by_status": json.dumps(self.excluded_by_status),
            }
        if "last_event" in sql:
            return self.operations
        if "INSERT INTO research_notes" in sql:
            return note_row(
                symbol=args[0],
                earnings_date=args[1],
                author=args[2],
                body=args[3],
                tags=args[4],
            )
        return self.events[0] if self.events else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql_seen.append(sql)
        if sql.strip() == "SELECT 1":
            return 1
        if "parameters" in sql:
            return self.parameters
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.sql_seen.append(sql)
        return "OK"


class _Acquire:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *_args: object) -> bool:
        return False


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    async def close(self) -> None:
        return None

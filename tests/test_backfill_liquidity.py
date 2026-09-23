from __future__ import annotations

import datetime as dt
from typing import Any

from schwab_gateway_sdk.models import HistoryResponseV1

from afterhours_lab import backfill_liquidity
from afterhours_lab.archive_earnings import MIN_AVG_DOLLAR_VOLUME

UTC = dt.timezone.utc
DATE_A = dt.date(2026, 9, 9)
DATE_B = dt.date(2026, 9, 10)

LIQUID_AVG_DOLLAR_VOLUME = 100_000_000.0


def daily_history(symbol: str, *, avg_dollar_volume: float) -> HistoryResponseV1:
    close = 100.0
    volume = int(avg_dollar_volume / close)
    bar = {
        "timestamp": dt.datetime(2026, 9, 8, 20, tzinfo=UTC),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }
    return HistoryResponseV1.model_validate(
        {
            "schema_version": "1.0",
            "history": {
                "symbol": symbol,
                "frequency": "daily",
                "bars": [bar] * 10,
                "gateway_received_at": dt.datetime(2026, 9, 9, tzinfo=UTC),
                "source": "schwab",
                "stale": False,
                "age_seconds": 0,
                "data_quality_flags": [],
            },
        }
    )


class FakeGateway:
    """Every symbol is liquid unless named in `avg_dollar_volume_by_symbol` or
    `error_symbols`. Records each requested symbol once per call."""

    def __init__(self, avg_dollar_volume_by_symbol=None, *, error_symbols=()):
        self._avg = avg_dollar_volume_by_symbol or {}
        self._error_symbols = set(error_symbols)
        self.requested_symbols: list[str] = []

    async def get_history(self, symbol, *, frequency="daily", days_back=None):
        self.requested_symbols.append(symbol)
        if symbol in self._error_symbols:
            raise RuntimeError("gateway unavailable")
        avg_dollar_volume = self._avg.get(symbol, LIQUID_AVG_DOLLAR_VOLUME)
        return daily_history(symbol, avg_dollar_volume=avg_dollar_volume)


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


async def test_flags_only_symbols_under_the_liquidity_floor() -> None:
    backlog = [
        backfill_liquidity.BacklogEvent(symbol="THIN", earnings_date=DATE_A),
        backfill_liquidity.BacklogEvent(symbol="LIQUID", earnings_date=DATE_B),
    ]
    gateway = FakeGateway({"THIN": MIN_AVG_DOLLAR_VOLUME - 1})
    conn = RecordingConnection()

    outcome = await backfill_liquidity.backfill_liquidity(conn, gateway, backlog)

    assert [event.symbol for event in outcome.excluded] == ["THIN"]
    assert outcome.kept_thin_check_inconclusive == ()
    assert outcome.checked_symbols == 2
    [(sql, args)] = conn.calls
    assert "UPDATE earnings_events" in sql
    assert "liquidity_excluded_at = now()" in sql
    assert args[0] == "THIN"
    assert args[1] == DATE_A
    assert "20,000,000" in args[2] or "$20,000,000" in args[2]


async def test_dry_run_reports_without_writing() -> None:
    backlog = [backfill_liquidity.BacklogEvent(symbol="THIN", earnings_date=DATE_A)]
    gateway = FakeGateway({"THIN": MIN_AVG_DOLLAR_VOLUME - 1})
    conn = RecordingConnection()

    outcome = await backfill_liquidity.backfill_liquidity(
        conn, gateway, backlog, dry_run=True
    )

    assert [event.symbol for event in outcome.excluded] == ["THIN"]
    assert conn.calls == []


async def test_inconclusive_check_keeps_the_event() -> None:
    """A gateway error or empty history must not flag an event: losing a real event
    to a transient gateway error is worse than leaving one thin name unflagged."""
    backlog = [
        backfill_liquidity.BacklogEvent(symbol="ERRORS", earnings_date=DATE_A),
        backfill_liquidity.BacklogEvent(symbol="LIQUID", earnings_date=DATE_B),
    ]
    gateway = FakeGateway(error_symbols=["ERRORS"])
    conn = RecordingConnection()

    outcome = await backfill_liquidity.backfill_liquidity(conn, gateway, backlog)

    assert outcome.excluded == ()
    assert outcome.kept_thin_check_inconclusive == ("ERRORS",)
    assert conn.calls == []


async def test_checks_each_distinct_symbol_only_once() -> None:
    backlog = [
        backfill_liquidity.BacklogEvent(symbol="REPEAT", earnings_date=DATE_A),
        backfill_liquidity.BacklogEvent(symbol="REPEAT", earnings_date=DATE_B),
    ]
    gateway = FakeGateway({"REPEAT": MIN_AVG_DOLLAR_VOLUME - 1})
    conn = RecordingConnection()

    outcome = await backfill_liquidity.backfill_liquidity(conn, gateway, backlog)

    assert gateway.requested_symbols == ["REPEAT"]
    assert len(outcome.excluded) == 2
    assert len(conn.calls) == 2


async def test_fetch_insufficient_data_backlog_filters_status_and_already_flagged() -> None:
    class FakeQueryConnection:
        def __init__(self, rows):
            self._rows = rows
            self.sql_seen: list[str] = []

        async def fetch(self, sql: str, *args: Any):
            self.sql_seen.append(sql)
            return self._rows

        async def fetchrow(self, sql: str, *args: Any):
            raise AssertionError("fetchrow should not be called")

    rows = [{"symbol": "THIN", "earnings_date": DATE_A}]
    conn = FakeQueryConnection(rows)

    backlog = await backfill_liquidity.fetch_insufficient_data_backlog(conn)

    assert backlog == [backfill_liquidity.BacklogEvent(symbol="THIN", earnings_date=DATE_A)]
    [sql] = conn.sql_seen
    assert "analysis_status = 'insufficient_data'" in sql
    assert "liquidity_excluded_at IS NULL" in sql

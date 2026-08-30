import datetime as dt

import pytest
from schwab_gateway_sdk.models import (
    HistoryResponseV1,
    QuoteResponseV1,
    SessionHistoryResponseV1,
)

from afterhours_lab.evidence import (
    preserve_minute_history,
    preserve_quotes,
    preserve_session_history,
)

RECEIVED = dt.datetime(2026, 8, 25, 20, 1, tzinfo=dt.timezone.utc)
EVENT = dt.datetime(2026, 8, 25, 20, 0, tzinfo=dt.timezone.utc)


class FakeConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.rows: list[tuple] = []

    async def executemany(self, sql: str, rows) -> None:
        self.sql = sql
        self.rows.extend(rows)


def quote_response() -> QuoteResponseV1:
    return QuoteResponseV1.model_validate(
        {
            "schema_version": "1.0",
            "quotes": [
                {
                    "symbol": "AAPL",
                    "event_timestamp": EVENT,
                    "gateway_received_at": RECEIVED,
                    "source": "schwab",
                    "session": "post_market",
                    "bid": 225.1,
                    "ask": 225.2,
                    "bid_size": 10,
                    "ask_size": 20,
                    "last": 225.15,
                    "last_size": 5,
                    "mark": 225.15,
                    "volume": 123456,
                    "close": 224.0,
                    "net_percent_change": 0.51,
                    "stale": False,
                    "age_seconds": 1.0,
                    "data_quality_flags": ["after_hours"],
                }
            ],
        }
    )


def history_response(*, frequency: str = "minute") -> HistoryResponseV1:
    return HistoryResponseV1.model_validate(
        {
            "schema_version": "1.0",
            "history": {
                "symbol": "AAPL",
                "frequency": frequency,
                "bars": [
                    {
                        "timestamp": EVENT,
                        "open": 225.0,
                        "high": 226.0,
                        "low": 224.5,
                        "close": 225.5,
                        "volume": 1200,
                    }
                ],
                "event_timestamp": EVENT,
                "gateway_received_at": RECEIVED,
                "source": "schwab",
                "stale": False,
                "age_seconds": 1.0,
                "data_quality_flags": [],
            },
        }
    )


async def test_quote_evidence_preserves_contract_and_marks_status_metadata_unknown() -> None:
    conn = FakeConnection()
    await preserve_quotes(
        conn,
        quote_response(),
        capture_window="after_hours",
        earnings_date=dt.date(2026, 8, 25),
    )

    row = conn.rows[0]
    assert "INSERT INTO quote_evidence" in conn.sql
    assert row[:21] == (
        "AAPL", EVENT, RECEIVED, "post_market", "after_hours", dt.date(2026, 8, 25),
        225.1, 225.2, 10, 20, 225.15, 5, 225.15, 123456, 224.0, 0.51,
        "schwab", False, 1.0,
        ["after_hours"], "1.0",
    )
    assert row[21:] == (None, None, None, None)


async def test_minute_history_preserves_ohlcv_provenance_and_unknown_session() -> None:
    conn = FakeConnection()
    await preserve_minute_history(conn, history_response())

    row = conn.rows[0]
    assert "INSERT INTO bar_evidence" in conn.sql
    assert row[:9] == (
        "AAPL", EVENT, "unknown", EVENT.date(), 225.0, 226.0, 224.5, 225.5, 1200
    )
    assert row[9:19] == (
        EVENT,
        RECEIVED,
        "schwab",
        "/v1/history",
        "minute",
        False,
        1.0,
        [],
        "1.0",
        "manual_collection",
    )
    assert row[19:] == (None, None, None, None)


async def test_daily_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="only minute"):
        await preserve_minute_history(FakeConnection(), history_response(frequency="daily"))


async def test_unknown_collection_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported bar collection mode"):
        await preserve_minute_history(
            FakeConnection(), history_response(), collection_mode="captured_live"
        )


async def test_extended_session_history_preserves_authoritative_session_and_date() -> None:
    payload = history_response().history.model_dump()
    payload.pop("frequency")
    payload["date"] = dt.date(2026, 8, 25)
    payload["session"] = "extended"
    payload["candles"] = payload.pop("bars")
    response = SessionHistoryResponseV1(
        schema_version="1.0", session_history=payload
    )
    conn = FakeConnection()

    await preserve_session_history(conn, response, collection_mode="historical_backfill")

    assert conn.rows[0][2:4] == ("extended", dt.date(2026, 8, 25))
    assert conn.rows[0][12:14] == ("/v1/session-history", "minute")
    assert conn.rows[0][18] == "historical_backfill"

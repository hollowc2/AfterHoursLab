import datetime as dt

import httpx
import pytest

from afterhours_lab.earnings import (
    EarningsCalendarClient,
    EarningsCalendarError,
    EarningsSettings,
    after_close_symbols,
)

CALENDAR_PAYLOAD = {
    "earningsCalendar": [
        {"symbol": "AAA", "date": "2026-08-19", "hour": "amc"},
        {"symbol": "BBB", "date": "2026-08-19", "hour": "bmo"},
        {"symbol": "CCC", "date": "2026-08-19", "hour": "dmh"},
        {"symbol": "DDD", "date": "2026-08-19", "hour": None},
        {"symbol": "AAA", "date": "2026-08-19", "hour": "amc"},
    ]
}


def make_settings() -> EarningsSettings:
    return EarningsSettings.model_validate({"FINNHUB_API_KEY": "test-key"})


async def test_get_earnings_calendar_parses_rows_and_sends_token() -> None:
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json=CALENDAR_PAYLOAD)

    transport = httpx.MockTransport(handler)
    fake_client = httpx.AsyncClient(base_url="https://finnhub.io/api/v1", transport=transport)

    client = EarningsCalendarClient(make_settings(), client=fake_client)
    try:
        entries = await client.get_earnings_calendar(dt.date(2026, 8, 19), dt.date(2026, 8, 19))
    finally:
        await fake_client.aclose()

    assert len(entries) == 5
    assert entries[0].symbol == "AAA"
    assert entries[0].is_after_close is True
    assert entries[1].is_after_close is False
    assert captured_params["token"] == "test-key"
    assert captured_params["from"] == "2026-08-19"


async def test_get_earnings_calendar_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    fake_client = httpx.AsyncClient(base_url="https://finnhub.io/api/v1", transport=transport)

    client = EarningsCalendarClient(make_settings(), client=fake_client)
    try:
        with pytest.raises(EarningsCalendarError):
            await client.get_earnings_calendar(dt.date(2026, 8, 19), dt.date(2026, 8, 19))
    finally:
        await fake_client.aclose()


async def test_get_earnings_calendar_raises_on_malformed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    transport = httpx.MockTransport(handler)
    fake_client = httpx.AsyncClient(base_url="https://finnhub.io/api/v1", transport=transport)

    client = EarningsCalendarClient(make_settings(), client=fake_client)
    try:
        with pytest.raises(EarningsCalendarError):
            await client.get_earnings_calendar(dt.date(2026, 8, 19), dt.date(2026, 8, 19))
    finally:
        await fake_client.aclose()


def test_after_close_symbols_filters_and_dedupes() -> None:
    entries_payload = CALENDAR_PAYLOAD["earningsCalendar"]
    from afterhours_lab.earnings import EarningsEntry

    entries = [EarningsEntry.model_validate(row) for row in entries_payload]
    assert after_close_symbols(entries) == ["AAA"]

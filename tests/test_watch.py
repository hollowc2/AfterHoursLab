import datetime as dt

import pytest
from schwab_gateway_sdk.client import GatewayUnavailableError
from schwab_gateway_sdk.models import QuoteResponseV1, QuoteV1

from afterhours_lab import watch

NOW = dt.datetime.now(dt.timezone.utc)


def make_quote(**overrides) -> QuoteV1:
    defaults = dict(
        symbol="SPY",
        gateway_received_at=NOW,
        source="test",
        stale=False,
        last=450.12,
        bid=450.10,
        ask=450.14,
        mark=450.12,
        volume=1000,
    )
    defaults.update(overrides)
    return QuoteV1(**defaults)


def test_quote_to_row_formats_prices_and_flags() -> None:
    quote = make_quote(data_quality_flags=("delayed",))
    row = watch.quote_to_row(quote)
    assert row == ("SPY", "450.12", "450.10", "450.14", "450.12", "1000", "live", "delayed")


def test_quote_to_row_handles_missing_values() -> None:
    quote = make_quote(last=None, bid=None, ask=None, mark=None, volume=None, stale=True)
    row = watch.quote_to_row(quote)
    assert row == ("SPY", "-", "-", "-", "-", "-", "stale", "-")


class FakeGateway:
    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)

    async def get_quotes(self, symbols: list[str]) -> QuoteResponseV1:
        item = next(self._responses)
        if isinstance(item, Exception):
            raise item
        return item


async def test_run_watch_polls_and_stops_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    response = QuoteResponseV1(quotes=(make_quote(),))
    gateway = FakeGateway([response, response])

    sleep_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise watch.asyncio.CancelledError()

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)

    with pytest.raises(watch.asyncio.CancelledError):
        await watch.run_watch(gateway, ["SPY"], interval_seconds=0)

    assert sleep_calls == 2


async def test_run_watch_backs_off_on_gateway_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = QuoteResponseV1(quotes=(make_quote(),))
    gateway = FakeGateway([GatewayUnavailableError("down"), response])

    sleep_delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_delays.append(seconds)
        if len(sleep_delays) >= 2:
            raise watch.asyncio.CancelledError()

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)

    with pytest.raises(watch.asyncio.CancelledError):
        await watch.run_watch(gateway, ["SPY"], interval_seconds=1)

    assert sleep_delays[0] == watch.BACKOFF_SECONDS

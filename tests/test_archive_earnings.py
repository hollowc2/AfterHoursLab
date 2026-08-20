import datetime as dt

import pytest

from afterhours_lab import archive_earnings
from afterhours_lab.earnings import EarningsCalendarError, EarningsEntry
from afterhours_lab.watchlist import load_watchlist

TODAY = dt.date(2026, 8, 19)  # Wednesday


class FakeEarningsClient:
    def __init__(self, entries=None, error=None) -> None:
        self._entries = entries or []
        self._error = error

    async def get_earnings_calendar(self, from_date, to_date):
        if self._error:
            raise self._error
        return self._entries

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def entries(*rows):
    return [EarningsEntry(symbol=s, date=TODAY, hour=h) for s, h in rows]


class FakeConnection:
    """Backs the (symbol, earnings_date) -> hour store the real earnings_events
    table would hold, with the same INSERT ... ON CONFLICT DO NOTHING and
    DISTINCT-symbol-in-range query shapes archive_earnings.py issues."""

    def __init__(self, store: dict) -> None:
        self._store = store

    async def executemany(self, _sql: str, args_list) -> None:
        for symbol, earnings_date, hour in args_list:
            self._store.setdefault((symbol, earnings_date), hour)

    async def fetch(self, _sql: str, window_start: dt.date, window_end: dt.date):
        symbols = sorted(
            {
                symbol
                for (symbol, earnings_date) in self._store
                if window_start <= earnings_date <= window_end
            }
        )
        return [{"symbol": symbol} for symbol in symbols]


class _FakeAcquire:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def __aenter__(self) -> FakeConnection:
        return FakeConnection(self._store)

    async def __aexit__(self, *_args) -> None:
        return None


def make_fake_pool_class(store: dict):
    class FakeDatabasePool:
        @classmethod
        async def connect(cls, _settings):
            return cls()

        def acquire(self):
            return _FakeAcquire(store)

        async def close(self) -> None:
            return None

    return FakeDatabasePool


def patch_common(monkeypatch, *, entries_result=None, error=None, store=None):
    store = store if store is not None else {}
    fake_client = FakeEarningsClient(entries_result, error)
    monkeypatch.setattr(archive_earnings, "EarningsCalendarClient", lambda settings: fake_client)
    monkeypatch.setattr(archive_earnings, "EarningsSettings", lambda: object())
    monkeypatch.setattr(archive_earnings, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(archive_earnings, "DatabasePool", make_fake_pool_class(store))
    return store


async def test_main_archives_after_close_symbols_into_watchlist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = patch_common(monkeypatch, entries_result=entries(("AAA", "amc"), ("BBB", "bmo")))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["AAA"]
    assert store == {("AAA", TODAY): "amc"}


async def test_main_dry_run_does_not_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = patch_common(monkeypatch, entries_result=entries(("AAA", "amc")))

    exit_code = await archive_earnings._main(
        ["--watchlist", str(watchlist_path), "--dry-run"], today=TODAY
    )

    assert exit_code == 0
    assert not watchlist_path.exists()
    assert store == {}


async def test_main_returns_error_code_on_gateway_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    patch_common(monkeypatch, error=EarningsCalendarError("boom"))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 1


async def test_main_keeps_symbol_within_active_window_with_no_new_earnings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol flagged yesterday (day_before window) should still show up today
    (its earnings/after_hours day) even if Finnhub returns nothing new."""
    watchlist_path = tmp_path / "watchlist.json"
    yesterday = archive_earnings._previous_trading_day(TODAY)
    store = patch_common(monkeypatch, entries_result=entries(), store={("ZZZ", yesterday): "amc"})

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["ZZZ"]
    assert store == {("ZZZ", yesterday): "amc"}


async def test_main_prunes_symbol_outside_active_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol whose earnings event has aged out of the day_before/day_after window
    must not linger in the watchlist forever."""
    watchlist_path = tmp_path / "watchlist.json"
    stale_date = TODAY - dt.timedelta(days=10)
    store = patch_common(monkeypatch, entries_result=entries(), store={("OLD", stale_date): "amc"})

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == []
    assert store == {("OLD", stale_date): "amc"}


async def test_main_does_not_overwrite_existing_earnings_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ON CONFLICT DO NOTHING: an already-recorded event (and its captured flags,
    modeled here just as the stored hour) must not be disturbed by a re-run."""
    watchlist_path = tmp_path / "watchlist.json"
    store = patch_common(
        monkeypatch,
        entries_result=entries(("AAA", "amc")),
        store={("AAA", TODAY): "bmo"},  # pretend it was already recorded differently
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert store[("AAA", TODAY)] == "bmo"


def test_previous_and_next_trading_day_skip_weekends() -> None:
    friday = dt.date(2026, 8, 21)
    assert friday.weekday() == 4
    monday = dt.date(2026, 8, 24)

    assert archive_earnings._next_trading_day(friday) == monday
    assert archive_earnings._previous_trading_day(monday) == friday

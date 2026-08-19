import datetime as dt

import pytest

from afterhours_lab import archive_earnings
from afterhours_lab.earnings import EarningsCalendarError, EarningsEntry
from afterhours_lab.watchlist import load_watchlist, save_watchlist


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
    return [EarningsEntry(symbol=s, date=dt.date(2026, 8, 19), hour=h) for s, h in rows]


async def test_main_archives_after_close_symbols_into_watchlist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    save_watchlist(["ZZZ"], watchlist_path)

    fake = FakeEarningsClient(entries(("AAA", "amc"), ("BBB", "bmo")))
    monkeypatch.setattr(archive_earnings, "EarningsCalendarClient", lambda settings: fake)
    monkeypatch.setattr(archive_earnings, "EarningsSettings", lambda: object())

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)])

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["ZZZ", "AAA"]


async def test_main_dry_run_does_not_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchlist_path = tmp_path / "watchlist.json"

    fake = FakeEarningsClient(entries(("AAA", "amc")))
    monkeypatch.setattr(archive_earnings, "EarningsCalendarClient", lambda settings: fake)
    monkeypatch.setattr(archive_earnings, "EarningsSettings", lambda: object())

    exit_code = await archive_earnings._main(
        ["--watchlist", str(watchlist_path), "--dry-run"]
    )

    assert exit_code == 0
    assert not watchlist_path.exists()


async def test_main_returns_error_code_on_gateway_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"

    fake = FakeEarningsClient(error=EarningsCalendarError("boom"))
    monkeypatch.setattr(archive_earnings, "EarningsCalendarClient", lambda settings: fake)
    monkeypatch.setattr(archive_earnings, "EarningsSettings", lambda: object())

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)])

    assert exit_code == 1

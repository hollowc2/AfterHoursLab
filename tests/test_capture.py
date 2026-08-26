import datetime as dt
import json

import pytest
from schwab_gateway_sdk.client import GatewayUnavailableError
from schwab_gateway_sdk.models import QuoteResponseV1, QuoteV1

from afterhours_lab import archive_earnings, capture
from afterhours_lab.status import status_path_for
from afterhours_lab.trading_calendar import next_trading_day

TODAY = dt.date(2026, 8, 19)  # Wednesday; window="after_hours" -> earnings_date == TODAY
MINUTE0 = dt.datetime(2026, 8, 19, 16, 0, 0, tzinfo=dt.timezone.utc)
MINUTE1 = MINUTE0 + dt.timedelta(minutes=1)

_COLUMN_NAMES = ("day_before_captured", "after_hours_captured", "day_after_captured")


def make_quote(**overrides) -> QuoteV1:
    defaults = dict(
        symbol="AAA",
        event_timestamp=MINUTE0,
        gateway_received_at=MINUTE0,
        source="test-src",
        session="regular",
        stale=False,
        last=100.0,
        volume=1000,
    )
    defaults.update(overrides)
    return QuoteV1(**defaults)


def event(*, day_before=False, after_hours=False, day_after=False) -> dict:
    return {
        "day_before_captured": day_before,
        "after_hours_captured": after_hours,
        "day_after_captured": day_after,
    }


class FakeGateway:
    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)
        self.calls: list[list[str]] = []

    async def get_quotes(self, symbols: list[str]) -> QuoteResponseV1:
        self.calls.append(list(symbols))
        item = next(self._responses)
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self) -> "FakeGateway":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _column_in(sql: str) -> str:
    return next(c for c in _COLUMN_NAMES if c in sql)


class FakeConnection:
    """Backs earnings_events (captured flags) and candles with the same query shapes
    capture.py issues: the not-yet-captured SELECT, the advisory lock calls, the
    candle INSERT ... ON CONFLICT DO NOTHING, and the final captured-flag UPDATE."""

    def __init__(self, store: dict, candles: list, *, lock_available: bool = True) -> None:
        self._store = store
        self._candles = candles
        self._lock_available = lock_available
        self.unlocked = False
        self.lock_keys: list[int] = []

    async def fetch(self, sql: str, *args: object):
        assert "SELECT symbol FROM earnings_events" in sql
        column = _column_in(sql)
        (earnings_date,) = args
        symbols = sorted(
            symbol
            for (symbol, ed), flags in self._store.items()
            if ed == earnings_date and not flags[column]
        )
        return [{"symbol": s} for s in symbols]

    async def fetchval(self, sql: str, *args: object) -> bool:
        assert sql.startswith("SELECT pg_try_advisory_lock")
        self.lock_keys.append(args[0])
        return self._lock_available

    async def execute(self, sql: str, *args: object) -> None:
        if sql.startswith("SELECT pg_advisory_unlock"):
            self.unlocked = True
            return
        assert sql.strip().startswith("UPDATE earnings_events")
        column = _column_in(sql)
        symbols, earnings_date = args
        for symbol in symbols:
            self._store[(symbol, earnings_date)][column] = True

    async def executemany(self, sql: str, rows) -> None:
        if sql.strip().startswith("INSERT INTO quote_evidence"):
            return
        assert sql.strip().startswith("INSERT INTO candles")
        self._candles.extend(rows)


class _FakeAcquire:
    def __init__(self, store: dict, candles: list, *, lock_available: bool) -> None:
        self._store = store
        self._candles = candles
        self._lock_available = lock_available
        self.connection: FakeConnection | None = None

    async def __aenter__(self) -> FakeConnection:
        self.connection = FakeConnection(
            self._store, self._candles, lock_available=self._lock_available
        )
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


def make_fake_pool_class(store: dict, candles: list, *, lock_available: bool = True):
    class FakeDatabasePool:
        acquisitions: list[_FakeAcquire] = []

        @classmethod
        async def connect(cls, _settings):
            return cls()

        def acquire(self):
            acquire = _FakeAcquire(store, candles, lock_available=lock_available)
            FakeDatabasePool.acquisitions.append(acquire)
            return acquire

        async def close(self) -> None:
            return None

    return FakeDatabasePool


def patch_common(
    monkeypatch,
    *,
    store: dict,
    candles: list | None = None,
    lock_available: bool = True,
    gateway_responses: list | None = None,
):
    candles = candles if candles is not None else []
    fake_pool_class = make_fake_pool_class(store, candles, lock_available=lock_available)
    monkeypatch.setattr(capture, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(capture, "DatabasePool", fake_pool_class)
    monkeypatch.setattr(capture, "AppSettings", lambda: object())
    gateway = FakeGateway(gateway_responses or [])
    monkeypatch.setattr(capture, "build_gateway_client", lambda _settings: gateway)
    return fake_pool_class, gateway, candles


def _status_path(watchlist_path):
    return status_path_for(watchlist_path, filename=capture.CAPTURE_STATUS_FILENAME)


async def _fake_sleep_recording(delays: list) -> None:
    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    return _sleep


async def test_main_aggregates_quotes_into_ohlcv_bars_and_marks_captured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", TODAY): event()}
    responses = [
        QuoteResponseV1(quotes=(make_quote(event_timestamp=MINUTE0, last=100.0, volume=1000),)),
        QuoteResponseV1(quotes=(make_quote(event_timestamp=MINUTE0, last=102.0, volume=1010),)),
        QuoteResponseV1(quotes=(make_quote(event_timestamp=MINUTE1, last=105.0, volume=1050),)),
    ]
    _, gateway, candles = patch_common(monkeypatch, store=store, gateway_responses=responses)

    sleep_delays: list[float] = []
    monkeypatch.setattr(capture.asyncio, "sleep", await _fake_sleep_recording(sleep_delays))

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "3",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    assert gateway.calls == [["AAA"]] * 3
    assert candles == [
        ("AAA", MINUTE0, "regular", "after_hours", TODAY,
         100.0, 102.0, 100.0, 102.0, 10, "test-src"),
        ("AAA", MINUTE1, "regular", "after_hours", TODAY,
         105.0, 105.0, 105.0, 105.0, 0, "test-src"),
    ]
    assert store[("AAA", TODAY)]["after_hours_captured"] is True

    status = json.loads(_status_path(watchlist_path).read_text())
    assert status["ok"] is True


async def test_already_captured_symbol_is_excluded_and_never_polled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = {
        ("AAA", TODAY): event(after_hours=False),
        ("BBB", TODAY): event(after_hours=True),
    }
    responses = [QuoteResponseV1(quotes=(make_quote(symbol="AAA", event_timestamp=MINUTE0),))]
    _, gateway, candles = patch_common(monkeypatch, store=store, gateway_responses=responses)

    sleep_delays: list[float] = []
    monkeypatch.setattr(capture.asyncio, "sleep", await _fake_sleep_recording(sleep_delays))

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "1",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    assert gateway.calls == [["AAA"]]
    assert store[("BBB", TODAY)]["after_hours_captured"] is True  # unchanged, still True
    assert store[("AAA", TODAY)]["after_hours_captured"] is True  # now captured


async def test_lock_already_held_skips_entire_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", TODAY): event()}
    fake_pool_class, gateway, candles = patch_common(
        monkeypatch, store=store, lock_available=False, gateway_responses=[]
    )

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "5",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    assert gateway.calls == []
    assert candles == []
    assert store[("AAA", TODAY)]["after_hours_captured"] is False

    status = json.loads(_status_path(watchlist_path).read_text())
    assert status["ok"] is True
    assert "already running" in status["detail"]


async def test_gateway_error_mid_run_is_caught_and_backed_off(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", TODAY): event()}
    responses = [
        GatewayUnavailableError("down"),
        QuoteResponseV1(quotes=(make_quote(event_timestamp=MINUTE0, last=100.0, volume=1000),)),
    ]
    _, gateway, candles = patch_common(monkeypatch, store=store, gateway_responses=responses)

    sleep_delays: list[float] = []
    monkeypatch.setattr(capture.asyncio, "sleep", await _fake_sleep_recording(sleep_delays))

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "2",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    assert len(gateway.calls) == 2
    assert sleep_delays[0] == capture.BACKOFF_SECONDS
    assert candles == [
        ("AAA", MINUTE0, "regular", "after_hours", TODAY,
         100.0, 100.0, 100.0, 100.0, 0, "test-src"),
    ]
    assert store[("AAA", TODAY)]["after_hours_captured"] is True


async def test_still_open_final_minute_is_flushed_not_dropped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", TODAY): event()}
    responses = [
        QuoteResponseV1(quotes=(make_quote(event_timestamp=MINUTE0, last=100.0, volume=500),)),
    ]
    _, gateway, candles = patch_common(monkeypatch, store=store, gateway_responses=responses)

    sleep_delays: list[float] = []
    monkeypatch.setattr(capture.asyncio, "sleep", await _fake_sleep_recording(sleep_delays))

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "1",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    # Never flushed mid-loop (only one bucket, always "current"); must be flushed
    # after the loop ends instead of being silently dropped.
    assert candles == [
        ("AAA", MINUTE0, "regular", "after_hours", TODAY,
         100.0, 100.0, 100.0, 100.0, 0, "test-src"),
    ]
    assert store[("AAA", TODAY)]["after_hours_captured"] is True


async def test_no_matching_symbols_exits_cleanly_without_locking(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store: dict = {}  # nothing pending for this earnings_date/window
    fake_pool_class, gateway, candles = patch_common(monkeypatch, store=store, gateway_responses=[])

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "5",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    assert gateway.calls == []
    assert candles == []
    # Only the pending-symbols lookup acquired a connection; the lock section
    # (a second acquire) never ran.
    assert len(fake_pool_class.acquisitions) == 1

    status = json.loads(_status_path(watchlist_path).read_text())
    assert status["ok"] is True
    assert "no symbols pending" in status["detail"]


def test_each_window_gets_its_own_lock_key() -> None:
    """The three windows must not share a key. day_after polls from 9:28 AM ET for
    395 minutes, straight through the 3:55 PM day_before and 4:00 PM after_hours
    fires; when all three shared one key those two took the "already running" branch
    and skipped — recorded as a successful run — every day a day_after was active."""
    keys = {window: capture.capture_lock_key(window) for window in capture._WINDOW_COLUMNS}
    assert len(set(keys.values())) == 3


def test_lock_keys_are_stable_and_distinct_from_the_other_apps_locks() -> None:
    """Offsets are pinned, not derived from dict order: a window's key sliding onto
    one a running process already holds would resurrect the bug above."""
    assert capture.capture_lock_key("day_before") == capture.CAPTURE_LOCK_KEY + 0
    assert capture.capture_lock_key("after_hours") == capture.CAPTURE_LOCK_KEY + 1
    assert capture.capture_lock_key("day_after") == capture.CAPTURE_LOCK_KEY + 2
    # Must not collide with the archive-earnings guard, which runs on the same host.
    assert archive_earnings.ARCHIVE_LOCK_KEY not in {
        capture.capture_lock_key(window) for window in capture._WINDOW_COLUMNS
    }


def test_lock_key_rejects_an_unknown_window() -> None:
    with pytest.raises(KeyError):
        capture.capture_lock_key("not_a_window")


async def test_run_requests_the_lock_key_for_its_own_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the key actually handed to pg_try_advisory_lock is this window's,
    not the shared base key."""
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", next_trading_day(TODAY)): event()}
    fake_pool_class, _gateway, _candles = patch_common(
        monkeypatch, store=store, gateway_responses=[]
    )

    exit_code = await capture._main(
        [
            "--window", "day_before",
            "--duration-minutes", "0",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    requested = [
        key
        for acquire in fake_pool_class.acquisitions
        if acquire.connection
        for key in acquire.connection.lock_keys
    ]
    assert requested == [capture.capture_lock_key("day_before")]


async def test_skip_message_names_the_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is per-window, so a skip means another run of *this* window is still
    going — the status detail has to say which, or the old ambiguous wording would
    still read as "some capture is busy"."""
    watchlist_path = tmp_path / "watchlist.json"
    store = {("AAA", TODAY): event()}
    patch_common(monkeypatch, store=store, lock_available=False, gateway_responses=[])

    exit_code = await capture._main(
        [
            "--window", "after_hours",
            "--duration-minutes", "5",
            "--interval-seconds", "60",
            "--watchlist", str(watchlist_path),
        ],
        today=TODAY,
    )

    assert exit_code == 0
    status = json.loads(_status_path(watchlist_path).read_text())
    assert status["detail"] == "after_hours capture already running; skipped this run"

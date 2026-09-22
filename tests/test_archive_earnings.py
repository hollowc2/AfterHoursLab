import datetime as dt
import json

import pytest
from schwab_gateway_sdk.models import HistoryResponseV1

from afterhours_lab import archive_earnings
from afterhours_lab.earnings import EarningsCalendarError, EarningsEntry
from afterhours_lab.status import status_path_for
from afterhours_lab.watchlist import load_watchlist, read_watchlist

TODAY = dt.date(2026, 8, 19)  # Wednesday
UTC = dt.timezone.utc

# Well above MIN_AVG_DOLLAR_VOLUME, so a FakeGateway with no override is liquid by
# default and every pre-existing test (written before the liquidity filter existed)
# keeps matching its symbols through unchanged.
LIQUID_AVG_DOLLAR_VOLUME = 100_000_000.0


def daily_history(symbol: str, *, avg_dollar_volume: float) -> HistoryResponseV1:
    close = 100.0
    volume = int(avg_dollar_volume / close)
    bar = {
        "timestamp": dt.datetime(2026, 8, 18, 20, tzinfo=UTC),
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
                "bars": [bar] * archive_earnings.LIQUIDITY_LOOKBACK_DAYS,
                "gateway_received_at": dt.datetime(2026, 8, 19, tzinfo=UTC),
                "source": "schwab",
                "stale": False,
                "age_seconds": 0,
                "data_quality_flags": [],
            },
        }
    )


class FakeGateway:
    """Every symbol is liquid unless named in `avg_dollar_volume_by_symbol` or
    `error_symbols`."""

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

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "FakeGateway":
        return self

    async def __aexit__(self, *_args) -> None:
        return None


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
    """Each row is either (symbol, hour) or (symbol, hour, extra_fields_dict), where
    extra_fields_dict may set eps_estimate/eps_actual/revenue_estimate/revenue_actual/
    quarter/year by name (EarningsEntry has populate_by_name=True)."""
    result = []
    for row in rows:
        if len(row) == 2:
            symbol, hour = row
            extra: dict = {}
        else:
            symbol, hour, extra = row
        result.append(EarningsEntry(symbol=symbol, date=TODAY, hour=hour, **extra))
    return result


def stored(hour=None, **overrides):
    """The full per-(symbol, earnings_date) record FakeConnection now stores,
    mirroring the earnings_events row shape: hour plus the six surprise-data
    columns, defaulting to None like a fresh INSERT would."""
    record = {
        "hour": hour,
        "eps_estimate": None,
        "eps_actual": None,
        "revenue_estimate": None,
        "revenue_actual": None,
        "quarter": None,
        "year": None,
    }
    record.update(overrides)
    return record


class FakeConnection:
    """Backs the (symbol, earnings_date) -> record store the real earnings_events
    table would hold, with the same INSERT ... ON CONFLICT DO UPDATE and
    DISTINCT-symbol-in-range query shapes archive_earnings.py issues, plus the
    pg_try_advisory_lock/pg_advisory_unlock calls the re-entrancy guard issues.

    Mirrors the real conflict semantics: `hour` is insert-only (never touched on
    conflict); eps_actual/revenue_actual prefer the new incoming value when
    non-null, else keep what's stored; eps_estimate/revenue_estimate/quarter/year
    prefer whatever is already stored when non-null, else take the new value."""

    def __init__(self, store: dict, *, lock_available: bool = True) -> None:
        self._store = store
        self._lock_available = lock_available
        self.unlocked = False

    async def fetchrow(
        self,
        _sql: str,
        symbol,
        earnings_date,
        hour,
        eps_estimate,
        eps_actual,
        revenue_estimate,
        revenue_actual,
        quarter,
        year,
    ) -> dict:
        """One RETURNING (xmax = 0) AS inserted row per call, mirroring the real
        per-row fetchrow loop _upsert_earnings_events now issues instead of a
        batched executemany."""
        key = (symbol, earnings_date)
        existing = self._store.get(key)
        inserted = existing is None
        if existing is None:
            self._store[key] = {
                "hour": hour,
                "eps_estimate": eps_estimate,
                "eps_actual": eps_actual,
                "revenue_estimate": revenue_estimate,
                "revenue_actual": revenue_actual,
                "quarter": quarter,
                "year": year,
            }
        else:
            # COALESCE semantics: an explicit None check, not truthiness, since 0.0
            # is a legitimate (falsy) estimate/actual value.
            if existing["eps_estimate"] is None:
                existing["eps_estimate"] = eps_estimate
            if eps_actual is not None:
                existing["eps_actual"] = eps_actual
            if existing["revenue_estimate"] is None:
                existing["revenue_estimate"] = revenue_estimate
            if revenue_actual is not None:
                existing["revenue_actual"] = revenue_actual
            if existing["quarter"] is None:
                existing["quarter"] = quarter
            if existing["year"] is None:
                existing["year"] = year
        return {"inserted": inserted}

    async def fetch(self, _sql: str, window_start: dt.date, window_end: dt.date):
        symbols = sorted(
            {
                symbol
                for (symbol, earnings_date) in self._store
                if window_start <= earnings_date <= window_end
            }
        )
        return [{"symbol": symbol} for symbol in symbols]

    async def fetchval(self, sql: str, *_args: object) -> bool:
        assert sql.startswith("SELECT pg_try_advisory_lock")
        return self._lock_available

    async def execute(self, sql: str, *_args: object) -> None:
        assert sql.startswith("SELECT pg_advisory_unlock")
        self.unlocked = True


class _FakeAcquire:
    def __init__(self, store: dict, *, lock_available: bool) -> None:
        self._store = store
        self._lock_available = lock_available
        self.connection: FakeConnection | None = None

    async def __aenter__(self) -> FakeConnection:
        self.connection = FakeConnection(self._store, lock_available=self._lock_available)
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


def make_fake_pool_class(store: dict, *, lock_available: bool = True):
    class FakeDatabasePool:
        acquisitions: list[_FakeAcquire] = []

        @classmethod
        async def connect(cls, _settings):
            return cls()

        def acquire(self):
            acquire = _FakeAcquire(store, lock_available=lock_available)
            FakeDatabasePool.acquisitions.append(acquire)
            return acquire

        async def close(self) -> None:
            return None

    return FakeDatabasePool


def patch_common(
    monkeypatch,
    *,
    entries_result=None,
    error=None,
    store=None,
    lock_available=True,
    gateway=None,
):
    store = store if store is not None else {}
    fake_client = FakeEarningsClient(entries_result, error)
    monkeypatch.setattr(archive_earnings, "EarningsCalendarClient", lambda settings: fake_client)
    monkeypatch.setattr(archive_earnings, "EarningsSettings", lambda: object())
    monkeypatch.setattr(archive_earnings, "DatabaseSettings", lambda: object())
    fake_pool_class = make_fake_pool_class(store, lock_available=lock_available)
    monkeypatch.setattr(archive_earnings, "DatabasePool", fake_pool_class)
    fake_gateway = gateway if gateway is not None else FakeGateway()
    monkeypatch.setattr(archive_earnings, "AppSettings", lambda: object())
    monkeypatch.setattr(archive_earnings, "build_gateway_client", lambda settings: fake_gateway)
    return store, fake_pool_class


async def test_liquidity_filter_drops_thin_symbols() -> None:
    gateway = FakeGateway({"THIN": 1_000_000.0, "LIQUID": 50_000_000.0})
    kept, dropped = await archive_earnings._filter_by_liquidity(
        gateway, entries(("THIN", "amc"), ("LIQUID", "amc"))
    )

    assert [entry.symbol for entry in kept] == ["LIQUID"]
    assert dropped == ["THIN"]


async def test_liquidity_filter_keeps_symbol_on_gateway_error() -> None:
    gateway = FakeGateway(error_symbols=["FLAKY"])
    kept, dropped = await archive_earnings._filter_by_liquidity(
        gateway, entries(("FLAKY", "amc"))
    )

    assert [entry.symbol for entry in kept] == ["FLAKY"]
    assert dropped == []


async def test_liquidity_filter_keeps_symbol_with_no_bars() -> None:
    class EmptyHistoryGateway:
        async def get_history(self, symbol, *, frequency="daily", days_back=None):
            return HistoryResponseV1.model_validate(
                {
                    "schema_version": "1.0",
                    "history": {
                        "symbol": symbol,
                        "frequency": "daily",
                        "bars": [],
                        "gateway_received_at": dt.datetime(2026, 8, 19, tzinfo=UTC),
                        "source": "schwab",
                        "stale": False,
                        "age_seconds": 0,
                        "data_quality_flags": [],
                    },
                }
            )

    kept, dropped = await archive_earnings._filter_by_liquidity(
        EmptyHistoryGateway(), entries(("NEW", "amc"))
    )

    assert [entry.symbol for entry in kept] == ["NEW"]
    assert dropped == []


async def test_main_drops_thin_symbol_before_archiving(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    gateway = FakeGateway({"THIN": 1_000_000.0})
    store, _ = patch_common(
        monkeypatch,
        entries_result=entries(("THIN", "amc"), ("LIQUID", "amc")),
        gateway=gateway,
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["LIQUID"]
    assert ("THIN", TODAY) not in store
    assert ("LIQUID", TODAY) in store


async def test_main_archives_after_close_symbols_into_watchlist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store, _ = patch_common(monkeypatch, entries_result=entries(("AAA", "amc"), ("BBB", "bmo")))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["AAA"]
    assert store == {("AAA", TODAY): stored("amc")}

    status = json.loads(status_path_for(watchlist_path).read_text())
    assert status["ok"] is True


async def test_main_dry_run_does_not_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    store, _ = patch_common(monkeypatch, entries_result=entries(("AAA", "amc")))

    exit_code = await archive_earnings._main(
        ["--watchlist", str(watchlist_path), "--dry-run"], today=TODAY
    )

    assert exit_code == 0
    assert not watchlist_path.exists()
    assert store == {}
    assert not status_path_for(watchlist_path).exists()


async def test_main_returns_error_code_on_gateway_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    patch_common(monkeypatch, error=EarningsCalendarError("boom"))
    notified = []
    monkeypatch.setattr(archive_earnings.notify, "send", lambda message: notified.append(message))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 1
    status = json.loads(status_path_for(watchlist_path).read_text())
    assert status["ok"] is False
    assert "boom" in status["detail"]
    assert len(notified) == 1
    assert "boom" in notified[0]


async def test_main_keeps_symbol_within_active_window_with_no_new_earnings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol flagged yesterday (day_before window) should still show up today
    (its earnings/after_hours day) even if Finnhub returns nothing new."""
    watchlist_path = tmp_path / "watchlist.json"
    yesterday = archive_earnings._previous_trading_day(TODAY)
    store, _ = patch_common(
        monkeypatch, entries_result=entries(), store={("ZZZ", yesterday): stored("amc")}
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == ["ZZZ"]
    assert store == {("ZZZ", yesterday): stored("amc")}


async def test_main_prunes_symbol_outside_active_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol whose earnings event has aged out of the day_before/day_after window
    must not linger in the watchlist forever."""
    watchlist_path = tmp_path / "watchlist.json"
    stale_date = TODAY - dt.timedelta(days=10)
    store, _ = patch_common(
        monkeypatch, entries_result=entries(), store={("OLD", stale_date): stored("amc")}
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert load_watchlist(watchlist_path) == []
    assert store == {("OLD", stale_date): stored("amc")}


async def test_main_does_not_overwrite_existing_earnings_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hour` is insert-only: an already-recorded event's scheduling window must not
    be disturbed by a re-run, even though the six surprise-data columns now do get
    updated on conflict (see the tests below)."""
    watchlist_path = tmp_path / "watchlist.json"
    store, _ = patch_common(
        monkeypatch,
        entries_result=entries(("AAA", "amc")),
        store={("AAA", TODAY): stored("bmo")},  # pretend it was already recorded differently
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert store[("AAA", TODAY)]["hour"] == "bmo"


async def test_upsert_earnings_events_stores_estimates_and_null_actuals() -> None:
    store: dict = {}
    conn = FakeConnection(store)
    row = entries(
        (
            "AAA",
            "amc",
            {"eps_estimate": 1.5, "revenue_estimate": 2_000_000.0, "quarter": 3, "year": 2026},
        )
    )[0]

    await archive_earnings._upsert_earnings_events(conn, [row])

    assert store == {
        ("AAA", TODAY): stored(
            "amc", eps_estimate=1.5, revenue_estimate=2_000_000.0, quarter=3, year=2026
        )
    }


async def test_upsert_earnings_events_fills_in_actuals_without_disturbing_hour_or_estimates() -> (
    None
):
    """A later run carrying actuals (estimates now null, since Finnhub stops
    reporting them once the print has happened) fills in eps_actual/revenue_actual
    while leaving the originally-recorded hour and estimates untouched."""
    store: dict = {
        ("AAA", TODAY): stored(
            "amc", eps_estimate=1.5, revenue_estimate=2_000_000.0, quarter=3, year=2026
        )
    }
    conn = FakeConnection(store)
    row = entries(("AAA", "bmo", {"eps_actual": 1.7, "revenue_actual": 2_100_000.0}))[0]

    await archive_earnings._upsert_earnings_events(conn, [row])

    assert store == {
        ("AAA", TODAY): stored(
            "amc",
            eps_estimate=1.5,
            eps_actual=1.7,
            revenue_estimate=2_000_000.0,
            revenue_actual=2_100_000.0,
            quarter=3,
            year=2026,
        )
    }


async def test_upsert_earnings_events_does_not_overwrite_stored_estimates() -> None:
    """A second upsert that also carries non-null estimates must not clobber the
    estimates already stored from the first run."""
    store: dict = {("AAA", TODAY): stored("amc", eps_estimate=1.5, quarter=3, year=2026)}
    conn = FakeConnection(store)
    row = entries(("AAA", "amc", {"eps_estimate": 9.9, "quarter": 1, "year": 2020}))[0]

    await archive_earnings._upsert_earnings_events(conn, [row])

    assert store[("AAA", TODAY)]["eps_estimate"] == 1.5
    assert store[("AAA", TODAY)]["quarter"] == 3
    assert store[("AAA", TODAY)]["year"] == 2026


async def test_upsert_earnings_events_returns_count_of_newly_inserted_rows() -> None:
    """A fresh batch — nothing previously recorded — reports every entry as newly
    inserted, not just len(entries) coincidentally matching."""
    store: dict = {}
    conn = FakeConnection(store)
    rows = entries(("AAA", "amc"), ("BBB", "bmo"))

    inserted_count = await archive_earnings._upsert_earnings_events(conn, rows)

    assert inserted_count == 2


async def test_upsert_earnings_events_returns_zero_when_all_already_recorded() -> None:
    """A rerun where every symbol was already recorded (e.g. a same-day refetch
    filling in actuals) must report 0 newly-inserted rows, even though the upsert
    still writes to every one of them."""
    store: dict = {
        ("AAA", TODAY): stored("amc"),
        ("BBB", TODAY): stored("bmo"),
    }
    conn = FakeConnection(store)
    rows = entries(("AAA", "amc"), ("BBB", "bmo"))

    inserted_count = await archive_earnings._upsert_earnings_events(conn, rows)

    assert inserted_count == 0


async def test_upsert_earnings_events_returns_only_genuinely_new_count_for_mixed_batch() -> None:
    store: dict = {("AAA", TODAY): stored("amc")}
    conn = FakeConnection(store)
    rows = entries(("AAA", "amc"), ("BBB", "bmo"))

    inserted_count = await archive_earnings._upsert_earnings_events(conn, rows)

    assert inserted_count == 1


async def test_main_success_message_reports_genuinely_new_count_not_len_matched(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the inaccurate-count finding: a rerun where the matched symbol was
    already recorded must report "archived 0 new event(s)", not len(matched)."""
    watchlist_path = tmp_path / "watchlist.json"
    store, _ = patch_common(
        monkeypatch,
        entries_result=entries(("AAA", "amc")),
        store={("AAA", TODAY): stored("amc")},
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    status = json.loads(status_path_for(watchlist_path).read_text())
    assert "archived 0 new event(s)" in status["detail"]
    assert store == {("AAA", TODAY): stored("amc")}


async def test_main_skips_when_another_run_holds_the_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck/overlapping run holding ARCHIVE_LOCK_KEY makes this run back off
    rather than racing it on _upsert_earnings_events / save_watchlist."""
    watchlist_path = tmp_path / "watchlist.json"
    store, _ = patch_common(
        monkeypatch, entries_result=entries(("AAA", "amc")), lock_available=False
    )

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    assert not watchlist_path.exists()
    assert store == {}  # never upserted either — the whole DB section was skipped
    status = json.loads(status_path_for(watchlist_path).read_text())
    assert status["ok"] is True
    assert "already running" in status["detail"]


async def test_main_unlocks_after_a_successful_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    _, fake_pool_class = patch_common(monkeypatch, entries_result=entries(("AAA", "amc")))

    await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert len(fake_pool_class.acquisitions) == 1
    assert fake_pool_class.acquisitions[0].connection.unlocked is True


async def test_main_records_failure_and_notifies_on_unexpected_exception(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the 2026-08-20 incident shape: an exception writing the watchlist
    (there, PermissionError) must not crash silently — it should be recorded in
    last_run_status.json and trigger a notify before propagating."""
    watchlist_path = tmp_path / "watchlist.json"
    patch_common(monkeypatch, entries_result=entries(("AAA", "amc")))
    notified = []
    monkeypatch.setattr(archive_earnings.notify, "send", lambda message: notified.append(message))

    def boom(_symbols, _path, *, as_of=None):
        raise PermissionError("boom")

    monkeypatch.setattr(archive_earnings, "save_watchlist", boom)

    with pytest.raises(PermissionError):
        await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    status = json.loads(status_path_for(watchlist_path).read_text())
    assert status["ok"] is False
    assert "boom" in status["detail"]
    assert len(notified) == 1
    assert "boom" in notified[0]


def test_previous_and_next_trading_day_skip_weekends() -> None:
    friday = dt.date(2026, 8, 21)
    assert friday.weekday() == 4
    monday = dt.date(2026, 8, 24)

    assert archive_earnings._next_trading_day(friday) == monday
    assert archive_earnings._previous_trading_day(monday) == friday


def test_default_date_range_spans_the_active_window() -> None:
    """The default fetch has to reach the *next* trading day, or the 3:55 PM
    day_before capture would never find tomorrow's after-close names in
    earnings_events; and back to the previous one, so yesterday's actuals get
    backfilled once they publish."""
    args = archive_earnings.parse_args([], TODAY)
    assert args.from_date == dt.date(2026, 8, 18)  # Tuesday
    assert args.to_date == dt.date(2026, 8, 20)  # Thursday


def test_default_date_range_skips_the_weekend() -> None:
    friday = dt.date(2026, 8, 21)
    args = archive_earnings.parse_args([], friday)
    assert args.from_date == dt.date(2026, 8, 20)  # Thursday
    assert args.to_date == dt.date(2026, 8, 24)  # Monday, not Saturday


def test_explicit_dates_still_override_the_defaults() -> None:
    args = archive_earnings.parse_args(["--from", "2026-01-05", "--to", "2026-01-09"], TODAY)
    assert args.from_date == dt.date(2026, 1, 5)
    assert args.to_date == dt.date(2026, 1, 9)


async def test_written_watchlist_is_stamped_with_the_runs_today(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """as_of comes from the run's own `today`, which is what lets a later reader tell
    a current watchlist from one left behind by a run that stopped happening."""
    watchlist_path = tmp_path / "watchlist.json"
    patch_common(monkeypatch, entries_result=entries(("AAA", "amc")))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    result = read_watchlist(watchlist_path)
    assert result.as_of == TODAY
    assert not result.is_stale(TODAY)


async def test_quiet_day_writes_an_empty_but_current_watchlist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day with no after-close names is a real answer, not a failure: the watchlist
    goes empty rather than keeping yesterday's symbols, and is still stamped current
    so nothing reports it as stale."""
    watchlist_path = tmp_path / "watchlist.json"
    patch_common(monkeypatch, entries_result=entries(("AAA", "bmo")))

    exit_code = await archive_earnings._main(["--watchlist", str(watchlist_path)], today=TODAY)

    assert exit_code == 0
    result = read_watchlist(watchlist_path)
    assert result.symbols == []
    assert result.as_of == TODAY

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import pytest
from schwab_gateway_sdk.client import (
    GatewayAuthenticationError,
    GatewayAuthorizationError,
    GatewayResponseError,
    GatewayUnavailableError,
)
from schwab_gateway_sdk.models import QuoteResponseV1, QuoteV1

from afterhours_lab import monitor

EASTERN = ZoneInfo("America/New_York")
MARKET_DATE = dt.date(2026, 8, 20)
RECEIVED = dt.datetime(2026, 8, 20, 20, 1, tzinfo=dt.timezone.utc)


class FakeConnection:
    def __init__(self, symbols=(), *, inserted_count=0, lock_available=True) -> None:
        self.symbols = tuple(symbols)
        self.inserted_count = inserted_count
        self.lock_available = lock_available
        self.executed: list[tuple[str, tuple]] = []
        self.transactions = 0
        self.fetch_calls = 0

    async def fetch(self, sql, *_args):
        assert "hour = 'amc'" in sql
        assert "liquidity_excluded_at IS NULL" in sql
        self.fetch_calls += 1
        return [{"symbol": symbol} for symbol in self.symbols]

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return self.lock_available
        assert "WITH inserted AS" in sql
        return self.inserted_count

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield


class FakeGateway:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    async def get_quotes(self, symbols):
        self.calls.append(tuple(symbols))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def close(self):
        self.closed = True


def make_quote(**overrides) -> QuoteV1:
    values = {
        "symbol": "AAA",
        "event_timestamp": None,
        "gateway_received_at": RECEIVED,
        "source": "gateway",
        "session": None,
        "bid": None,
        "ask": 101.0,
        "bid_size": None,
        "ask_size": 7,
        "last": 100.5,
        "last_size": None,
        "mark": None,
        "volume": None,
        "close": 99.0,
        "net_percent_change": 1.52,
        "stale": True,
        "age_seconds": None,
        "data_quality_flags": ("delayed",),
    }
    values.update(overrides)
    return QuoteV1(**values)


def test_active_window_handles_est_edt_weekends_and_holidays() -> None:
    config = monitor.MonitorConfig()
    assert monitor.active_window(dt.datetime(2026, 1, 5, 16, 0, tzinfo=EASTERN), config)
    assert monitor.active_window(dt.datetime(2026, 8, 20, 16, 0, tzinfo=EASTERN), config)
    assert monitor.active_window(dt.datetime(2026, 8, 20, 15, 50, tzinfo=EASTERN), config)
    assert monitor.active_window(dt.datetime(2026, 8, 20, 20, 15, tzinfo=EASTERN), config)
    assert not monitor.active_window(dt.datetime(2026, 8, 22, 16, 0, tzinfo=EASTERN), config)
    assert not monitor.active_window(dt.datetime(2026, 12, 25, 16, 0, tzinfo=EASTERN), config)


def test_next_boundary_caps_sleep_and_crosses_midnight() -> None:
    config = monitor.MonitorConfig(max_idle_sleep_seconds=300)
    before = dt.datetime(2026, 8, 20, 15, 49, tzinfo=EASTERN)
    after = dt.datetime(2026, 8, 20, 23, 59, tzinfo=EASTERN)
    assert monitor.seconds_to_next_boundary(before, config) == 60
    assert monitor.seconds_to_next_boundary(after, config) == 300


def test_quote_mapping_is_lossless_and_leaves_unknown_status_null() -> None:
    row = monitor.quote_to_row(make_quote(), schema_version="1.0", earnings_date=MARKET_DATE)
    assert row.event_timestamp is None
    assert row.bid is None and row.ask == 101.0
    assert row.close == 99.0 and row.net_percent_change == 1.52
    assert row.stale and row.age_seconds is None
    assert row.data_quality_flags == ("delayed",)
    assert row.exto_eligible is None and row.exchange_status is None
    assert len(row.sql_args()) == 25


async def test_insert_counts_idempotency_conflicts_in_one_transaction() -> None:
    conn = FakeConnection(inserted_count=1)
    rows = [
        monitor.quote_to_row(make_quote(), schema_version="1.0", earnings_date=MARKET_DATE),
        monitor.quote_to_row(
            make_quote(symbol="BBB"), schema_version="1.0", earnings_date=MARKET_DATE
        ),
    ]
    assert await monitor.insert_quote_rows(conn, rows) == (1, 1)
    assert conn.transactions == 1
    quote_inserts = [entry for entry in conn.executed if "WITH inserted AS" in entry[0]]
    assert len(quote_inserts) == 1
    sql, args = quote_inserts[0]
    assert "$1" in sql and "$50" in sql
    assert len(args) == 50


async def test_cycle_batches_candidates_and_records_counts() -> None:
    response = QuoteResponseV1(quotes=(make_quote(), make_quote(symbol="BBB")))
    conn = FakeConnection(symbols=("AAA", "BBB"), inserted_count=1)
    gateway = FakeGateway(response)

    def now():
        return RECEIVED

    result = await monitor.run_cycle(conn, gateway, MARKET_DATE, now=now)

    assert gateway.calls == [("AAA", "BBB")]
    assert result == monitor.CycleResult(MARKET_DATE, 2, 2, 1, 1)
    assert any("INSERT INTO monitor_cycles" in sql for sql, _ in conn.executed)


async def test_no_candidates_never_calls_gateway() -> None:
    conn = FakeConnection()
    gateway = FakeGateway(AssertionError("must not be called"))
    result = await monitor.run_cycle(conn, gateway, MARKET_DATE, now=lambda: RECEIVED)
    assert result.fetched_count == 0
    assert gateway.calls == []


async def test_daemon_backs_off_and_records_no_candidates_only_once() -> None:
    conn = FakeConnection()
    gateway = FakeGateway(AssertionError("must not be called"))
    stop = asyncio.Event()
    sleeps = []

    async def sleeper(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            stop.set()

    code = await monitor.run_daemon(
        conn,
        gateway,
        monitor.MonitorConfig(interval_seconds=5, max_idle_sleep_seconds=300),
        stop=stop,
        now=lambda: RECEIVED,
        sleeper=sleeper,
    )

    assert code == 0
    assert sleeps == [300, 300]
    assert conn.fetch_calls == 2
    no_candidate_rows = [
        args
        for sql, args in conn.executed
        if "INSERT INTO monitor_cycles" in sql and args[4] == "no_candidates"
    ]
    assert len(no_candidate_rows) == 1
    assert gateway.calls == []


async def test_transient_failure_is_disclosed_and_backed_off() -> None:
    conn = FakeConnection(symbols=("AAA",))
    gateway = FakeGateway(GatewayUnavailableError("down"))
    stop = asyncio.Event()
    sleeps = []

    async def sleeper(delay):
        sleeps.append(delay)
        stop.set()

    code = await monitor.run_daemon(
        conn,
        gateway,
        monitor.MonitorConfig(interval_seconds=5),
        stop=stop,
        now=lambda: RECEIVED,
        sleeper=sleeper,
        once=False,
        forced_date=MARKET_DATE,
    )
    assert code == 0
    assert sleeps == [5]
    cycle_args = [args for sql, args in conn.executed if "monitor_cycles" in sql][-1]
    assert cycle_args[4] == "degraded"
    assert cycle_args[-2:] == ("GatewayUnavailableError", "gateway request failed")


@pytest.mark.parametrize(
    "error",
    [
        GatewayAuthenticationError("unauthenticated"),
        GatewayAuthorizationError("forbidden"),
        GatewayResponseError("malformed contract"),
    ],
)
async def test_once_terminal_failure_returns_nonzero_without_retry(error) -> None:
    conn = FakeConnection(symbols=("AAA",))
    gateway = FakeGateway(error)
    code = await monitor.run_daemon(
        conn,
        gateway,
        monitor.MonitorConfig(),
        stop=asyncio.Event(),
        now=lambda: RECEIVED,
        once=True,
        forced_date=MARKET_DATE,
    )
    assert code == 1


async def test_stop_interrupts_a_long_idle_sleep_promptly() -> None:
    conn = FakeConnection()
    gateway = FakeGateway(AssertionError("must not be called"))
    stop = asyncio.Event()

    async def never_finishes(_delay):
        await asyncio.Future()

    task = asyncio.create_task(
        monitor.run_daemon(
            conn,
            gateway,
            monitor.MonitorConfig(),
            stop=stop,
            now=lambda: dt.datetime(2026, 8, 22, 16, 0, tzinfo=EASTERN),
            sleeper=never_finishes,
        )
    )
    await asyncio.sleep(0)
    stop.set()

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert gateway.calls == []


class FakePool:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def close(self):
        self.closed = True


def patch_main(monkeypatch, conn, gateway):
    pool = FakePool(conn)

    class PoolFactory:
        @classmethod
        async def connect(cls, _settings):
            return pool

    monkeypatch.setattr(monitor, "DatabasePool", PoolFactory)
    monkeypatch.setattr(monitor, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(monitor, "AppSettings", lambda: object())
    monkeypatch.setattr(monitor, "build_gateway_client", lambda _settings: gateway)
    return pool


async def test_duplicate_instance_exits_before_constructing_gateway(monkeypatch) -> None:
    conn = FakeConnection(lock_available=False)
    pool = FakePool(conn)

    class PoolFactory:
        @classmethod
        async def connect(cls, _settings):
            return pool

    monkeypatch.setattr(monitor, "DatabasePool", PoolFactory)
    monkeypatch.setattr(monitor, "DatabaseSettings", lambda: object())

    def must_not_build(_settings):
        raise AssertionError("duplicate instance constructed a gateway client")

    monkeypatch.setattr(monitor, "build_gateway_client", must_not_build)

    assert await monitor._main(["--once", "--date", str(MARKET_DATE)]) == 0
    assert pool.closed
    assert not any("pg_advisory_unlock" in sql for sql, _ in conn.executed)


async def test_once_runs_full_lifecycle_and_closes_resources(monkeypatch) -> None:
    response = QuoteResponseV1(quotes=(make_quote(),))
    conn = FakeConnection(symbols=("AAA",), inserted_count=1)
    gateway = FakeGateway(response)
    pool = patch_main(monkeypatch, conn, gateway)

    assert await monitor._main(["--once", "--date", str(MARKET_DATE)]) == 0

    assert gateway.calls == [("AAA",)]
    assert gateway.closed
    assert pool.closed
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.executed)
    assert any("INSERT INTO monitor_cycles" in sql for sql, _ in conn.executed)

"""Always-on raw quote-evidence monitor for archived after-close candidates."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import signal
from collections.abc import Callable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from schwab_gateway_sdk.client import (
    GatewayCapacityError,
    GatewayClientError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)
from schwab_gateway_sdk.models import QuoteResponseV1, QuoteV1

from afterhours_lab.config import AppSettings
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.gateway import build_gateway_client
from afterhours_lab.research import fetch_monitor_candidates
from afterhours_lab.trading_calendar import is_trading_day

log = structlog.get_logger()

EASTERN = ZoneInfo("America/New_York")
CAPTURE_WINDOW = "live_reaction_monitor"
MONITOR_LOCK_KEY = 0x41484C4D4F4E4954  # "AHLMONIT"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_WINDOW_START = dt.time(15, 50)
DEFAULT_WINDOW_END = dt.time(20, 15)
MAX_IDLE_SLEEP_SECONDS = 300.0
MAX_FAILURE_BACKOFF_SECONDS = 60.0
_TRANSIENT_GATEWAY_ERRORS = (
    GatewayCapacityError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)


@dataclasses.dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    window_start: dt.time = DEFAULT_WINDOW_START
    window_end: dt.time = DEFAULT_WINDOW_END
    max_idle_sleep_seconds: float = MAX_IDLE_SLEEP_SECONDS
    max_failure_backoff_seconds: float = MAX_FAILURE_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval must be greater than zero")
        if self.max_idle_sleep_seconds <= 0:
            raise ValueError("maximum idle sleep must be greater than zero")
        if self.max_failure_backoff_seconds <= 0:
            raise ValueError("maximum failure backoff must be greater than zero")
        if self.window_start >= self.window_end:
            raise ValueError("window start must be earlier than window end")


@dataclasses.dataclass(frozen=True)
class QuoteEvidenceRow:
    symbol: str
    event_timestamp: dt.datetime | None
    gateway_received_at: dt.datetime
    session: str | None
    capture_window: str
    earnings_date: dt.date
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    last: float | None
    last_size: int | None
    mark: float | None
    volume: int | None
    close: float | None
    net_percent_change: float | None
    source: str
    stale: bool
    age_seconds: float | None
    data_quality_flags: tuple[str, ...]
    schema_version: str
    exto_eligible: bool | None = None
    exchange_status: str | None = None
    trading_status: str | None = None
    session_eligible: bool | None = None

    def sql_args(self) -> tuple[Any, ...]:
        values = dataclasses.astuple(self)
        return (*values[:19], list(self.data_quality_flags), *values[20:])


@dataclasses.dataclass(frozen=True)
class CycleResult:
    market_date: dt.date
    candidate_count: int
    fetched_count: int
    inserted_count: int
    conflicted_count: int


def quote_to_row(
    quote: QuoteV1, *, schema_version: str, earnings_date: dt.date
) -> QuoteEvidenceRow:
    """Map only gateway-contract fields; unknown status fields remain NULL."""
    return QuoteEvidenceRow(
        symbol=quote.symbol,
        event_timestamp=quote.event_timestamp,
        gateway_received_at=quote.gateway_received_at,
        session=quote.session,
        capture_window=CAPTURE_WINDOW,
        earnings_date=earnings_date,
        bid=quote.bid,
        ask=quote.ask,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        last=quote.last,
        last_size=quote.last_size,
        mark=quote.mark,
        volume=quote.volume,
        close=quote.close,
        net_percent_change=quote.net_percent_change,
        source=quote.source,
        stale=quote.stale,
        age_seconds=quote.age_seconds,
        data_quality_flags=quote.data_quality_flags,
        schema_version=schema_version,
    )


def active_window(now: dt.datetime, config: MonitorConfig) -> bool:
    eastern = now.astimezone(EASTERN)
    return (
        is_trading_day(eastern.date())
        and config.window_start <= eastern.time().replace(tzinfo=None) <= config.window_end
    )


def seconds_to_next_boundary(now: dt.datetime, config: MonitorConfig) -> float:
    """Bound idle sleeps so date and clock changes are re-evaluated frequently."""
    eastern = now.astimezone(EASTERN)
    day = eastern.date()
    local_time = eastern.time().replace(tzinfo=None)
    if is_trading_day(day) and local_time < config.window_start:
        boundary = dt.datetime.combine(day, config.window_start, EASTERN)
    else:
        day += dt.timedelta(days=1)
        while not is_trading_day(day):
            day += dt.timedelta(days=1)
        boundary = dt.datetime.combine(day, config.window_start, EASTERN)
    return max(0.0, min(config.max_idle_sleep_seconds, (boundary - eastern).total_seconds()))


_INSERT_QUOTE_PREFIX = """
    INSERT INTO quote_evidence (
        symbol, event_timestamp, gateway_received_at, session, capture_window,
        earnings_date, bid, ask, bid_size, ask_size, last, last_size, mark,
        volume, close, net_percent_change, source, stale, age_seconds,
        data_quality_flags, schema_version, exto_eligible, exchange_status,
        trading_status, session_eligible
    ) VALUES
"""


async def insert_quote_rows(conn, rows: Sequence[QuoteEvidenceRow]) -> tuple[int, int]:
    """Insert an entire gateway response in one statement and one transaction."""
    if not rows:
        return 0, 0
    width = len(rows[0].sql_args())
    values_sql: list[str] = []
    args: list[Any] = []
    for row_index, row in enumerate(rows):
        row_args = row.sql_args()
        if len(row_args) != width:
            raise ValueError("quote evidence rows have inconsistent widths")
        start = row_index * width + 1
        values_sql.append(
            "(" + ", ".join(f"${index}" for index in range(start, start + width)) + ")"
        )
        args.extend(row_args)
    sql = (
        "WITH inserted AS ("
        + _INSERT_QUOTE_PREFIX
        + ", ".join(values_sql)
        + " ON CONFLICT (symbol, gateway_received_at, capture_window, earnings_date) "
        + "DO NOTHING RETURNING 1) SELECT count(*)::int FROM inserted"
    )
    async with conn.transaction():
        inserted = await conn.fetchval(sql, *args)
    return inserted, len(rows) - inserted


async def record_cycle(
    conn,
    *,
    market_date: dt.date,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    active: bool,
    status: str,
    candidate_count: int = 0,
    fetched_count: int = 0,
    inserted_count: int = 0,
    conflicted_count: int = 0,
    error: BaseException | None = None,
) -> None:
    # Deliberately avoid persisting an exception's raw text: upstream HTTP errors may
    # include request details. The class and this bounded description are actionable
    # without creating a secret-bearing operational table.
    error_kind = type(error).__name__ if error else None
    error_message = "gateway request failed" if error else None
    await conn.execute(
        """
        INSERT INTO monitor_cycles (
            market_date, cycle_started_at, cycle_completed_at, active_window, status,
            candidate_count, fetched_quote_count, inserted_quote_count,
            conflicted_quote_count, error_kind, error_message
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        market_date,
        started_at,
        completed_at,
        active,
        status,
        candidate_count,
        fetched_count,
        inserted_count,
        conflicted_count,
        error_kind,
        error_message,
    )


async def run_cycle(
    conn,
    gateway,
    market_date: dt.date,
    *,
    now: Callable[[], dt.datetime],
    report_no_candidates: bool = True,
) -> CycleResult:
    started_at = now()
    symbols = await fetch_monitor_candidates(conn, market_date)
    if not symbols:
        if report_no_candidates:
            completed_at = now()
            await record_cycle(
                conn,
                market_date=market_date,
                started_at=started_at,
                completed_at=completed_at,
                active=True,
                status="no_candidates",
            )
            log.info("monitor_no_candidates", market_date=str(market_date))
        return CycleResult(market_date, 0, 0, 0, 0)

    response: QuoteResponseV1 = await gateway.get_quotes(symbols)
    rows = tuple(
        quote_to_row(quote, schema_version=response.schema_version, earnings_date=market_date)
        for quote in response.quotes
    )
    inserted, conflicted = await insert_quote_rows(conn, rows)
    completed_at = now()
    await record_cycle(
        conn,
        market_date=market_date,
        started_at=started_at,
        completed_at=completed_at,
        active=True,
        status="success",
        candidate_count=len(symbols),
        fetched_count=len(rows),
        inserted_count=inserted,
        conflicted_count=conflicted,
    )
    log.info(
        "monitor_cycle_complete",
        market_date=str(market_date),
        candidates=len(symbols),
        fetched=len(rows),
        inserted=inserted,
        conflicted=conflicted,
    )
    return CycleResult(market_date, len(symbols), len(rows), inserted, conflicted)


async def run_daemon(
    conn,
    gateway,
    config: MonitorConfig,
    *,
    stop: asyncio.Event,
    now: Callable[[], dt.datetime],
    sleeper: Callable[[float], Any] = asyncio.sleep,
    once: bool = False,
    forced_date: dt.date | None = None,
) -> int:
    async def sleep_or_stop(delay: float) -> None:
        """Sleep without making SIGTERM wait for the full idle/backoff interval."""
        sleep_task = asyncio.create_task(sleeper(delay))
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    failures = 0
    inactive_recorded_for: dt.date | None = None
    no_candidates_recorded_for: dt.date | None = None
    while not stop.is_set():
        current = now().astimezone(EASTERN)
        market_date = forced_date or current.date()
        active = once or active_window(current, config)
        if not active:
            if inactive_recorded_for != market_date:
                await record_cycle(
                    conn,
                    market_date=market_date,
                    started_at=current,
                    completed_at=current,
                    active=False,
                    status="inactive",
                )
                inactive_recorded_for = market_date
            await sleep_or_stop(seconds_to_next_boundary(current, config))
            continue

        inactive_recorded_for = None
        try:
            result = await run_cycle(
                conn,
                gateway,
                market_date,
                now=now,
                report_no_candidates=no_candidates_recorded_for != market_date,
            )
            failures = 0
        except GatewayClientError as exc:
            completed = now()
            candidate_count = len(await fetch_monitor_candidates(conn, market_date))
            await record_cycle(
                conn,
                market_date=market_date,
                started_at=completed,
                completed_at=completed,
                active=True,
                status="degraded",
                candidate_count=candidate_count,
                error=exc,
            )
            log.error("monitor_gateway_failed", error_kind=type(exc).__name__)
            if not isinstance(exc, _TRANSIENT_GATEWAY_ERRORS):
                return 1
            failures += 1
            if once:
                return 1
            delay = min(
                config.max_failure_backoff_seconds,
                config.interval_seconds * (2 ** min(failures - 1, 8)),
            )
            await sleep_or_stop(delay)
            continue

        if once:
            return 0
        if result.candidate_count == 0:
            no_candidates_recorded_for = market_date
            await sleep_or_stop(config.max_idle_sleep_seconds)
            continue
        no_candidates_recorded_for = None
        await sleep_or_stop(config.interval_seconds)
    return 0


def _parse_time(value: str) -> dt.time:
    try:
        parsed = dt.time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}; expected HH:MM") from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError("window times must be Eastern wall-clock times")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one cycle immediately")
    parser.add_argument("--date", type=dt.date.fromisoformat, help="market date for --once")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--window-start", type=_parse_time, default=DEFAULT_WINDOW_START)
    parser.add_argument("--window-end", type=_parse_time, default=DEFAULT_WINDOW_END)
    return parser


async def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.date and not args.once:
        raise SystemExit("--date requires --once; daemon mode always recomputes today's date")
    config = MonitorConfig(
        interval_seconds=args.interval_seconds,
        window_start=args.window_start,
        window_end=args.window_end,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows event loops
            pass

    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            async with try_advisory_lock(conn, MONITOR_LOCK_KEY) as acquired:
                if not acquired:
                    log.info("monitor_already_running")
                    return 0
                gateway = build_gateway_client(AppSettings())
                try:
                    return await run_daemon(
                        conn,
                        gateway,
                        config,
                        stop=stop,
                        now=lambda: dt.datetime.now(dt.timezone.utc),
                        once=args.once,
                        forced_date=args.date,
                    )
                finally:
                    await gateway.close()
    finally:
        await pool.close()


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":  # pragma: no cover
    main()

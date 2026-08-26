"""CLI: poll the gateway for quotes during one earnings capture window (day_before /
after_hours / day_after), aggregate the ticks into 1-min OHLCV candles, persist them to
`candles`, and flip the matching `{window}_captured` flag on `earnings_events`.

There is no historical-candles endpoint on the gateway — quotes are the only
market-data primitive available — so candles are built here by aggregating repeated
`get_quotes` polls over the run's duration. This script doesn't know about market
hours or timezones; a cron entry supplies the right --window/--duration-minutes pair
for each scheduled slot (e.g. one fire at the after-hours session open).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import sys
import time
from pathlib import Path

import structlog
from schwab_gateway_sdk.client import GatewayClientError, GatewayMarketDataClient
from schwab_gateway_sdk.models import QuoteV1

from afterhours_lab import notify
from afterhours_lab.config import AppSettings
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.evidence import preserve_quotes
from afterhours_lab.gateway import build_gateway_client
from afterhours_lab.status import status_path_for, write_status
from afterhours_lab.trading_calendar import next_trading_day, previous_trading_day
from afterhours_lab.watchlist import DEFAULT_WATCHLIST_PATH

log = structlog.get_logger()

DEFAULT_INTERVAL_SECONDS = 5.0
BACKOFF_SECONDS = 15.0

CAPTURE_STATUS_FILENAME = "capture_status.json"

# The only three capture_window values the `candles` table and the earnings_events
# `*_captured` columns understand. Always look the column name up through this dict —
# never string-interpolate the raw --window CLI value into SQL.
_WINDOW_COLUMNS = {
    "day_before": "day_before_captured",
    "after_hours": "after_hours_captured",
    "day_after": "day_after_captured",
}

# Re-entrancy guard: a third fixed advisory-lock base key, distinct from
# db/migrate.py's ADVISORY_LOCK_KEY and archive_earnings.py's ARCHIVE_LOCK_KEY, so a
# stuck capture run (this script holds a DB connection open for the whole polling
# window, possibly minutes to hours) can't race a fresh cron fire. Non-blocking
# (pg_try_advisory_lock): if a previous run still holds it, this run skips rather than
# queuing behind a possibly wedged process.
CAPTURE_LOCK_KEY = 0x41484C4341505452  # the 8 ASCII bytes of "AHLCAPTR" as an int64

# One lock key per window, not one shared across all three. The three windows overlap
# on the clock: day_after starts at 9:28 AM ET and polls for 395 minutes (through the
# 4:00 PM close), while day_before fires at 3:55 PM and after_hours at 4:00 PM — both
# inside day_after's run. A single shared key meant those two took the "already
# running" branch and skipped, recorded as a successful run, on every day a day_after
# capture was active. Each window now only guards against a stuck run of *itself*,
# which is what the guard was ever meant to do.
#
# Offsets are written out rather than derived from _WINDOW_COLUMNS' iteration order:
# reordering or inserting a window there must not silently slide an existing window's
# key onto the one a running process already holds.
_WINDOW_LOCK_OFFSETS = {
    "day_before": 0,
    "after_hours": 1,
    "day_after": 2,
}


def capture_lock_key(window: str) -> int:
    """The advisory-lock key guarding `window` against a concurrent run of itself."""
    return CAPTURE_LOCK_KEY + _WINDOW_LOCK_OFFSETS[window]


@dataclasses.dataclass
class _Bar:
    """In-memory accumulator for one (symbol, minute) candle-in-progress.

    `session` and `source` are captured once, from the tick that opened the bar —
    both are expected to stay constant across a single symbol's quotes within one
    minute, so there's no meaningful "most recent wins" choice to make here.
    """

    open: float
    high: float
    low: float
    close: float
    first_volume: int | None
    last_volume: int | None
    session: str
    source: str

    def update(self, *, last: float, volume: int | None) -> None:
        self.high = max(self.high, last)
        self.low = min(self.low, last)
        self.close = last
        self.last_volume = volume


def _new_bar(quote: QuoteV1) -> _Bar:
    last = quote.last
    assert last is not None  # callers only invoke this for quotes with a last price
    return _Bar(
        open=last,
        high=last,
        low=last,
        close=last,
        first_volume=quote.volume,
        last_volume=quote.volume,
        session=quote.session or "unknown",
        source=quote.source,
    )


def _volume_delta(bar: _Bar) -> int | None:
    """Best-effort tick volume for the bar.

    This assumes QuoteV1.volume is a cumulative running session total (typical of
    real-time last-sale feeds) rather than a per-tick trade size, so the bar's volume
    is last-seen minus first-seen. That assumption isn't verified against gateway
    docs; if it's wrong for a given source, this quietly falls back to NULL below
    (any inconsistency, e.g. a counter reset mid-window, shows up as a negative delta
    and is treated as unknown rather than recorded wrong).
    """
    if bar.first_volume is None or bar.last_volume is None:
        return None
    delta = bar.last_volume - bar.first_volume
    return delta if delta >= 0 else None


def _bucket_minute(quote: QuoteV1) -> dt.datetime:
    ts = quote.event_timestamp or quote.gateway_received_at
    return ts.replace(second=0, microsecond=0)


async def _flush_ready(
    conn,
    bars: dict[tuple[str, dt.datetime], _Bar],
    *,
    window: str,
    earnings_date: dt.date,
    before: dt.datetime | None,
) -> None:
    """Persist and drop every bar whose minute is strictly before `before`.

    `before=None` flushes everything still in memory, used at the end of the run to
    persist the still-open final minute along with everything else left over.
    """
    ready_keys = [key for key in bars if before is None or key[1] < before]
    if not ready_keys:
        return
    rows = []
    for key in ready_keys:
        symbol, minute = key
        bar = bars[key]
        rows.append(
            (
                symbol,
                minute,
                bar.session,
                window,
                earnings_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                _volume_delta(bar),
                bar.source,
            )
        )
    await conn.executemany(
        """
        INSERT INTO candles (
            symbol, ts, session, capture_window, earnings_date,
            open, high, low, close, volume, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (symbol, ts, session) DO NOTHING
        """,
        rows,
    )
    for key in ready_keys:
        del bars[key]


async def _poll_and_capture(
    gateway: GatewayMarketDataClient,
    conn,
    symbols: list[str],
    *,
    window: str,
    earnings_date: dt.date,
    duration_minutes: float,
    interval_seconds: float,
) -> set[str]:
    bars: dict[tuple[str, dt.datetime], _Bar] = {}
    observed_symbols: set[str] = set()
    deadline = time.monotonic() + duration_minutes * 60.0

    # Iteration count, not wall-clock duration math: the loop runs this many
    # poll-then-sleep cycles and stops, so a test can drive it deterministically by
    # monkeypatching asyncio.sleep instead of waiting out a real duration.
    iterations = max(0, round((duration_minutes * 60.0) / interval_seconds))

    for _ in range(iterations):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = await gateway.get_quotes(symbols)
        except GatewayClientError as exc:
            log.error("capture_poll_failed", error=str(exc))
            await asyncio.sleep(min(BACKOFF_SECONDS, remaining))
            continue

        # Preserve the exact quote contract before deriving minute bars from it.
        # This retains bid/ask/mark, both gateway timestamps, and provenance that an
        # OHLCV aggregate necessarily loses.
        await preserve_quotes(
            conn,
            response,
            capture_window=window,
            earnings_date=earnings_date,
        )

        current_minute: dt.datetime | None = None
        for quote in response.quotes:
            if quote.last is None:
                continue
            observed_symbols.add(quote.symbol)
            minute = _bucket_minute(quote)
            if current_minute is None or minute > current_minute:
                current_minute = minute
            key = (quote.symbol, minute)
            bar = bars.get(key)
            if bar is None:
                bars[key] = _new_bar(quote)
            else:
                bar.update(last=quote.last, volume=quote.volume)

        if current_minute is not None:
            await _flush_ready(
                conn, bars, window=window, earnings_date=earnings_date, before=current_minute
            )

        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(interval_seconds, remaining))

    # The window has ended: persist everything still in memory, including the
    # current, still-open minute.
    await _flush_ready(conn, bars, window=window, earnings_date=earnings_date, before=None)
    return observed_symbols


async def _mark_captured(conn, symbols: list[str], *, window: str, earnings_date: dt.date) -> None:
    column = _WINDOW_COLUMNS[window]
    await conn.execute(
        f"UPDATE earnings_events SET {column} = TRUE WHERE symbol = ANY($1) AND earnings_date = $2",
        symbols,
        earnings_date,
    )


async def _pending_symbols(pool: DatabasePool, window: str, earnings_date: dt.date) -> list[str]:
    column = _WINDOW_COLUMNS[window]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT symbol FROM earnings_events
            WHERE earnings_date = $1 AND {column} = FALSE
            ORDER BY symbol
            """,
            earnings_date,
        )
    return [row["symbol"] for row in rows]


async def _run_locked(
    pool: DatabasePool,
    gateway: GatewayMarketDataClient,
    symbols: list[str],
    *,
    window: str,
    earnings_date: dt.date,
    duration_minutes: float,
    interval_seconds: float,
) -> list[str] | None:
    """Poll, aggregate, and mark symbols captured, all on one connection held for the
    entire run under a non-blocking advisory lock. Returns None if another run of this
    same window already holds the lock (Postgres advisory locks are session-scoped, so
    a single connection has to span acquire, the poll loop, and the flag update).

    The lock is per-window (see capture_lock_key), so a long-running day_after poll
    doesn't lock out the day_before and after_hours runs that fire during it."""
    async with pool.acquire() as conn:
        async with try_advisory_lock(conn, capture_lock_key(window)) as acquired:
            if not acquired:
                return None
            observed_symbols = await _poll_and_capture(
                gateway,
                conn,
                symbols,
                window=window,
                earnings_date=earnings_date,
                duration_minutes=duration_minutes,
                interval_seconds=interval_seconds,
            )
            captured = [symbol for symbol in symbols if symbol in observed_symbols]
            if captured:
                await _mark_captured(
                    conn, captured, window=window, earnings_date=earnings_date
                )
            return captured


def _earnings_date_for_window(window: str, today: dt.date) -> dt.date:
    if window == "day_before":
        return next_trading_day(today)
    if window == "after_hours":
        return today
    return previous_trading_day(today)  # day_after


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Poll the gateway for quotes during one earnings capture window and "
            "persist the aggregated 1-min candles"
        )
    )
    parser.add_argument(
        "--window",
        required=True,
        choices=sorted(_WINDOW_COLUMNS),
        help="which capture window this run is for",
    )
    parser.add_argument(
        "--duration-minutes",
        required=True,
        type=float,
        help="how long to poll before stopping and flushing remaining candles",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between gateway polls",
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help="path to the persisted watchlist file (used only to locate the status file)",
    )
    return parser.parse_args(argv)


def _record_failure(status_path: Path, detail: str) -> None:
    try:
        write_status(status_path, ok=False, detail=detail)
    except OSError as exc:
        log.error("status_write_failed", error=str(exc))
    # notify.send() returns None when Telegram isn't configured (expected, not a
    # problem) vs False for a genuine delivery failure — only the latter is worth a
    # warning; capture_status.json above is already the failure record either way.
    if notify.send(f"AfterHoursLab capture FAILED: {detail}") is False:
        log.warning("notify_send_failed")


def _record_success(status_path: Path, detail: str) -> None:
    try:
        write_status(status_path, ok=True, detail=detail)
    except OSError as exc:
        log.error("status_write_failed", error=str(exc))


async def _main(argv: list[str], *, today: dt.date | None = None) -> int:
    today = today if today is not None else dt.date.today()
    args = parse_args(argv)
    status_path = status_path_for(args.watchlist, filename=CAPTURE_STATUS_FILENAME)
    earnings_date = _earnings_date_for_window(args.window, today)

    # Mirrors archive_earnings.py's shape: any exception past this point also lands
    # in capture_status.json and, if configured, a Telegram alert, before re-raising
    # (the log still gets the full traceback — this doesn't swallow the failure, just
    # stops it being silent).
    try:
        db_settings = DatabaseSettings()
        pool = await DatabasePool.connect(db_settings)
        try:
            symbols = await _pending_symbols(pool, args.window, earnings_date)
            if not symbols:
                msg = f"no symbols pending {args.window} capture for {earnings_date}"
                print(msg)
                _record_success(status_path, msg)
                return 0

            settings = AppSettings()
            async with build_gateway_client(settings) as gateway:
                captured = await _run_locked(
                    pool,
                    gateway,
                    symbols,
                    window=args.window,
                    earnings_date=earnings_date,
                    duration_minutes=args.duration_minutes,
                    interval_seconds=args.interval_seconds,
                )
        finally:
            await pool.close()

        if captured is None:
            # Names the window: the lock is per-window now, so this means another
            # run of *this* window is still going, not merely "some capture is busy".
            msg = f"{args.window} capture already running; skipped this run"
            print(msg)
            _record_success(status_path, msg)
            return 0

        if args.duration_minutes > 0 and not captured:
            raise RuntimeError(
                f"no usable quotes captured for {args.window} on {earnings_date}"
            )

        print(f"captured {args.window} candles ({earnings_date}): {captured}")
        _record_success(
            status_path,
            f"captured {len(captured)} symbol(s) for {args.window} on {earnings_date}",
        )
        return 0
    except Exception as exc:
        _record_failure(status_path, f"unexpected error: {exc}")
        raise


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

"""CLI: fetch after-close earnings for a date range, record them in earnings_events,
and rewrite the persisted watchlist to the current active window."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import structlog

from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.earnings import (
    EarningsCalendarClient,
    EarningsCalendarError,
    EarningsEntry,
    EarningsSettings,
    after_close_entries,
)
from afterhours_lab.watchlist import DEFAULT_WATCHLIST_PATH, save_watchlist

log = structlog.get_logger()

# The capture window around an earnings event: the trading day before, the earnings
# day itself, and the trading day after. A symbol stays "active" (watchable) while
# today falls anywhere in that window for its earnings_date.
_WEEKEND = (5, 6)


def _previous_trading_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() in _WEEKEND:
        day -= dt.timedelta(days=1)
    return day


def _next_trading_day(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() in _WEEKEND:
        day += dt.timedelta(days=1)
    return day


def parse_args(argv: list[str], today: dt.date) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive after-close earnings into earnings_events and refresh the watchlist"
    )
    parser.add_argument("--from", dest="from_date", type=dt.date.fromisoformat, default=today)
    parser.add_argument("--to", dest="to_date", type=dt.date.fromisoformat, default=today)
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help="path to the persisted watchlist file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print matching symbols without writing to earnings_events or the watchlist",
    )
    return parser.parse_args(argv)


async def _upsert_earnings_events(pool: DatabasePool, entries: list[EarningsEntry]) -> None:
    if not entries:
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO earnings_events (symbol, earnings_date, hour)
            VALUES ($1, $2, $3)
            ON CONFLICT (symbol, earnings_date) DO NOTHING
            """,
            [(entry.symbol, entry.date, entry.hour) for entry in entries],
        )


async def _active_symbols(pool: DatabasePool, today: dt.date) -> list[str]:
    window_start = _previous_trading_day(today)
    window_end = _next_trading_day(today)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT symbol FROM earnings_events
            WHERE earnings_date BETWEEN $1 AND $2
            ORDER BY symbol
            """,
            window_start,
            window_end,
        )
    return [row["symbol"] for row in rows]


async def _main(argv: list[str], *, today: dt.date | None = None) -> int:
    today = today if today is not None else dt.date.today()
    args = parse_args(argv, today)

    settings = EarningsSettings()
    async with EarningsCalendarClient(settings) as earnings:
        try:
            calendar_entries = await earnings.get_earnings_calendar(args.from_date, args.to_date)
        except EarningsCalendarError as exc:
            log.error("earnings_calendar_fetch_failed", error=str(exc))
            return 1

    matched = after_close_entries(calendar_entries)
    if matched:
        symbols = [entry.symbol for entry in matched]
        print(f"after-close earnings ({args.from_date}..{args.to_date}): {symbols}")
    else:
        print(f"no after-close earnings found for {args.from_date}..{args.to_date}")

    if args.dry_run:
        return 0

    db_settings = DatabaseSettings()
    pool = await DatabasePool.connect(db_settings)
    try:
        await _upsert_earnings_events(pool, matched)
        active = await _active_symbols(pool, today)
    finally:
        await pool.close()

    save_watchlist(active, args.watchlist)
    print(f"watchlist ({args.watchlist}): {active}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

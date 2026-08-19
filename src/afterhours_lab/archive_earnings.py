"""CLI: fetch after-close earnings for a date range and archive the symbols to the
persisted watchlist."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import structlog

from afterhours_lab.earnings import (
    EarningsCalendarClient,
    EarningsCalendarError,
    EarningsSettings,
    after_close_symbols,
)
from afterhours_lab.watchlist import DEFAULT_WATCHLIST_PATH, load_watchlist, save_watchlist

log = structlog.get_logger()


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = dt.date.today()
    parser = argparse.ArgumentParser(
        description="Archive symbols with after-close earnings into the persisted watchlist"
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
        help="print matching symbols without writing to the watchlist",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)

    settings = EarningsSettings()
    async with EarningsCalendarClient(settings) as earnings:
        try:
            entries = await earnings.get_earnings_calendar(args.from_date, args.to_date)
        except EarningsCalendarError as exc:
            log.error("earnings_calendar_fetch_failed", error=str(exc))
            return 1

    symbols = after_close_symbols(entries)
    if not symbols:
        print(f"no after-close earnings found for {args.from_date}..{args.to_date}")
        return 0

    print(f"after-close earnings ({args.from_date}..{args.to_date}): {symbols}")
    if args.dry_run:
        return 0

    existing = load_watchlist(args.watchlist)
    merged = existing + [s for s in symbols if s not in existing]
    save_watchlist(merged, args.watchlist)
    print(f"watchlist ({args.watchlist}): {merged}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

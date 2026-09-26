"""Retire earnings_events rows the earnings calendar has since moved to another date.

Events are keyed on (symbol, earnings_date), but the upstream calendar re-dates a
fiscal quarter's print as the company firms it up. Every move used to leave the
earlier date behind as a phantom event that was captured, monitored, and counted in
the research cohort (ANAB's 2026 Q2 was recorded on 2026-08-27, 2026-09-11, and
2026-09-21; the calendar now lists only 2026-09-21, with actuals).

A print's identity is (symbol, fiscal year, quarter). A recorded event is superseded
only on positive evidence: the calendar currently lists that same fiscal quarter on a
different date. A symbol merely absent from a response is never enough, so a partial
upstream response cannot retire real events. Rows are flagged
(``superseded_at``/``superseded_reason``/``superseded_by_date``), never deleted. If
the calendar later moves a quarter back to a date it had superseded, that row is
reinstated so the quarter always keeps its one live event.

``archive-earnings`` runs this on every refresh; the ``afterhours-lab-reconcile-calendar``
CLI applies it to events recorded before it existed.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence

import structlog

from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.earnings import (
    EarningsCalendarClient,
    EarningsCalendarError,
    EarningsEntry,
    EarningsSettings,
)

log = structlog.get_logger()

# How far either side of a recorded date to look for the quarter's current listing.
# Calendar moves seen so far are within a month; a quarter is ~90 days apart, so this
# also sees the neighbouring quarters without reaching two quarters away.
LOOKUP_RADIUS_DAYS = 75

# Finnhub's free tier allows 60 calls/min; the backlog CLI paces itself under it.
BACKLOG_LOOKUP_PAUSE_SECONDS = 1.1

# The 8 ASCII bytes of "AHLRECAL" as an int64, distinct from every other key here.
RECONCILE_LOCK_KEY = 0x41484C524543414C

Lookup = Callable[[str, dt.date, dt.date], Awaitable[list[EarningsEntry]]]


@dataclasses.dataclass(frozen=True)
class RecordedEvent:
    symbol: str
    earnings_date: dt.date
    year: int | None
    quarter: int | None
    superseded: bool = False


@dataclasses.dataclass(frozen=True)
class Supersession:
    symbol: str
    earnings_date: dt.date
    superseded_by_date: dt.date
    reason: str


@dataclasses.dataclass(frozen=True)
class ReconcilePlan:
    supersede: tuple[Supersession, ...]
    reinstate: tuple[RecordedEvent, ...]


def plan_reconciliation(
    recorded: Iterable[RecordedEvent], listed: Iterable[EarningsEntry]
) -> ReconcilePlan:
    """Pure: which recorded events the current listings supersede or reinstate.

    A fiscal quarter the listings place on more than one date is ambiguous and left
    alone, as is any event without a fiscal year and quarter.
    """
    listed_dates: dict[tuple[str, int, int], set[dt.date]] = {}
    for entry in listed:
        if entry.year is None or entry.quarter is None:
            continue
        listed_dates.setdefault((entry.symbol, entry.year, entry.quarter), set()).add(entry.date)
    canonical = {key: next(iter(dates)) for key, dates in listed_dates.items() if len(dates) == 1}

    supersede: list[Supersession] = []
    reinstate: list[RecordedEvent] = []
    for event in sorted(recorded, key=lambda item: (item.symbol, item.earnings_date)):
        if event.year is None or event.quarter is None:
            continue
        listed_date = canonical.get((event.symbol, event.year, event.quarter))
        if listed_date is None:
            continue
        if event.earnings_date == listed_date:
            if event.superseded:
                reinstate.append(event)
        elif not event.superseded:
            supersede.append(
                Supersession(
                    symbol=event.symbol,
                    earnings_date=event.earnings_date,
                    superseded_by_date=listed_date,
                    reason=(
                        f"earnings calendar lists {event.year} Q{event.quarter} "
                        f"on {listed_date.isoformat()}"
                    ),
                )
            )
    return ReconcilePlan(tuple(supersede), tuple(reinstate))


def _recorded(rows) -> list[RecordedEvent]:
    return [
        RecordedEvent(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            year=row["year"],
            quarter=row["quarter"],
            superseded=row["superseded_at"] is not None,
        )
        for row in rows
    ]


async def fetch_recorded_events(conn, symbols: Sequence[str]) -> list[RecordedEvent]:
    """Every after-close event (live or superseded) recorded for ``symbols``."""
    if not symbols:
        return []
    rows = await conn.fetch(
        """
        SELECT symbol, earnings_date, year, quarter, superseded_at
        FROM earnings_events
        WHERE hour = 'amc' AND symbol = ANY($1::text[])
        ORDER BY symbol, earnings_date
        """,
        list(symbols),
    )
    return _recorded(rows)


async def fetch_live_events_in_range(
    conn, from_date: dt.date, to_date: dt.date
) -> list[RecordedEvent]:
    rows = await conn.fetch(
        """
        SELECT symbol, earnings_date, year, quarter, superseded_at
        FROM earnings_events
        WHERE hour = 'amc' AND superseded_at IS NULL
          AND earnings_date BETWEEN $1 AND $2
        ORDER BY symbol, earnings_date
        """,
        from_date,
        to_date,
    )
    return _recorded(rows)


async def apply_plan(conn, plan: ReconcilePlan) -> None:
    for item in plan.supersede:
        await conn.execute(
            """
            UPDATE earnings_events
               SET superseded_at = now(), superseded_reason = $3, superseded_by_date = $4
             WHERE symbol = $1 AND earnings_date = $2 AND superseded_at IS NULL
            """,
            item.symbol,
            item.earnings_date,
            item.reason,
            item.superseded_by_date,
        )
    for event in plan.reinstate:
        await conn.execute(
            """
            UPDATE earnings_events
               SET superseded_at = NULL, superseded_reason = NULL, superseded_by_date = NULL
             WHERE symbol = $1 AND earnings_date = $2
            """,
            event.symbol,
            event.earnings_date,
        )


async def lookup_listings(
    events: Iterable[RecordedEvent], lookup: Lookup, *, pause_seconds: float = 0.0
) -> list[EarningsEntry]:
    """Each event's symbol's current listings around its recorded date(s).

    A failed lookup is logged and skipped: without evidence nothing is superseded.
    """
    by_symbol: dict[str, list[dt.date]] = {}
    for event in events:
        by_symbol.setdefault(event.symbol, []).append(event.earnings_date)
    radius = dt.timedelta(days=LOOKUP_RADIUS_DAYS)
    listed: list[EarningsEntry] = []
    for index, (symbol, dates) in enumerate(sorted(by_symbol.items())):
        if index and pause_seconds:
            await asyncio.sleep(pause_seconds)
        try:
            entries = await lookup(symbol, min(dates) - radius, max(dates) + radius)
        except EarningsCalendarError as exc:
            log.warning("calendar_lookup_failed", symbol=symbol, error=str(exc))
            continue
        listed.extend(entry for entry in entries if entry.symbol == symbol)
    return listed


async def reconcile(
    conn,
    listed: Sequence[EarningsEntry],
    *,
    window: tuple[dt.date, dt.date],
    lookup: Lookup,
    dry_run: bool = False,
) -> ReconcilePlan:
    """Reconcile after an archive refresh that fetched ``listed`` for ``window``.

    Recorded events inside the window that the fetched calendar no longer lists on
    their date get a per-symbol lookup, so a quarter that moved outside the window is
    still caught before its phantom date is captured.
    """
    listed_keys = {(entry.symbol, entry.date) for entry in listed}
    unlisted = [
        event
        for event in await fetch_live_events_in_range(conn, *window)
        if (event.symbol, event.earnings_date) not in listed_keys
    ]
    evidence = [*listed, *await lookup_listings(unlisted, lookup)]
    symbols = sorted({entry.symbol for entry in evidence})
    plan = plan_reconciliation(await fetch_recorded_events(conn, symbols), evidence)
    if not dry_run:
        await apply_plan(conn, plan)
    return plan


def describe(plan: ReconcilePlan, *, dry_run: bool = False) -> list[str]:
    supersede, reinstate = (
        ("would supersede", "would reinstate") if dry_run else ("superseded", "reinstated")
    )
    lines = [
        f"{supersede} {item.symbol} {item.earnings_date}: {item.reason}" for item in plan.supersede
    ]
    lines += [
        f"{reinstate} {event.symbol} {event.earnings_date}: calendar lists it again"
        for event in plan.reinstate
    ]
    return lines


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Supersede recorded earnings events the calendar has moved to another date"
    )
    parser.add_argument("--from", dest="from_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must be on or before --to")
    return args


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with EarningsCalendarClient(EarningsSettings()) as calendar:

            async def lookup(symbol: str, from_date: dt.date, to_date: dt.date):
                return await calendar.get_earnings_calendar(from_date, to_date, symbol=symbol)

            async with pool.acquire() as conn:
                async with try_advisory_lock(conn, RECONCILE_LOCK_KEY) as acquired:
                    if not acquired:
                        print("another reconcile-calendar run holds the lock; skipping")
                        return 0
                    events = await fetch_live_events_in_range(conn, args.from_date, args.to_date)
                    listed = await lookup_listings(
                        events, lookup, pause_seconds=BACKLOG_LOOKUP_PAUSE_SECONDS
                    )
                    symbols = sorted({entry.symbol for entry in listed})
                    plan = plan_reconciliation(await fetch_recorded_events(conn, symbols), listed)
                    if not args.dry_run:
                        await apply_plan(conn, plan)
    finally:
        await pool.close()
    for line in describe(plan, dry_run=args.dry_run):
        print(line)
    prefix = "dry run; would have " if args.dry_run else ""
    print(
        f"checked {len(events)} event(s) across {len({e.symbol for e in events})} symbol(s); "
        f"{prefix}superseded {len(plan.supersede)}, reinstated {len(plan.reinstate)}"
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

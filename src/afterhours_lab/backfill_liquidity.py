"""Retroactively flag thin-liquidity events already in the insufficient_data backlog.

``archive-earnings`` (see ``archive_earnings.MIN_AVG_DOLLAR_VOLUME``) keeps new
thin-liquidity candidates out of ``earnings_events`` entirely, but that change was
forward-only: it never touched events already recorded. Most of those existing
thin names never got postmarket bars from the gateway, so they sit in the research
cohort permanently as ``insufficient_data`` noise rather than as a real reaction
that's just missing data.

This tool applies the same $20M floor to that existing backlog. It never deletes or
rewrites a row: it sets ``liquidity_excluded_at``/``liquidity_excluded_reason`` on
``earnings_events``, which the research layer (``EventFilter.scope_sql``) excludes
from cohorts, quality issues, and distributions. Every underlying evidence and
feature row is untouched and remains queryable directly.

Only current liquidity is checkable here (the gateway's daily-history endpoint has
no historical end-date parameter), so this is a proxy for liquidity at the time of
each event, not an exact historical measure.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from dataclasses import dataclass

from afterhours_lab.archive_earnings import MIN_AVG_DOLLAR_VOLUME, check_liquidity
from afterhours_lab.config import AppSettings
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.gateway import BoundedGatewayClient, build_gateway_client
from afterhours_lab.reactions import CLASSIFIER_VERSION, DETECTOR_VERSION
from afterhours_lab.research.filters import FEATURE_VERSION

# Distinct from archive_earnings.ARCHIVE_LOCK_KEY and every other advisory-lock key
# in this codebase: the 8 ASCII bytes of "AHLLIQBF" as an int64.
BACKFILL_LOCK_KEY = 0x41484C4C49514246


@dataclass(frozen=True)
class BacklogEvent:
    symbol: str
    earnings_date: dt.date


@dataclass(frozen=True)
class BackfillOutcome:
    excluded: tuple[BacklogEvent, ...]
    kept_thin_check_inconclusive: tuple[str, ...]
    checked_symbols: int


async def fetch_insufficient_data_backlog(conn) -> list[BacklogEvent]:
    rows = await conn.fetch(
        """
        SELECT e.symbol, e.earnings_date
        FROM earnings_events e
        JOIN earnings_reaction_features f
          ON f.symbol = e.symbol AND f.earnings_date = e.earnings_date
         AND f.feature_version = $1 AND f.detector_version = $2 AND f.classifier_version = $3
        WHERE f.analysis_status = 'insufficient_data'
          AND e.liquidity_excluded_at IS NULL
        ORDER BY e.symbol, e.earnings_date
        """,
        FEATURE_VERSION,
        DETECTOR_VERSION,
        CLASSIFIER_VERSION,
    )
    return [BacklogEvent(symbol=row["symbol"], earnings_date=row["earnings_date"]) for row in rows]


async def backfill_liquidity(
    conn,
    gateway: BoundedGatewayClient,
    backlog: list[BacklogEvent],
    *,
    dry_run: bool = False,
) -> BackfillOutcome:
    """Check each distinct symbol's current trailing liquidity once, then flag every
    backlog event for symbols under the floor. Kept, not flagged, when the check
    itself is inconclusive (fetch failure or no bars) — the same bias as
    ``archive_earnings._filter_by_liquidity``: losing a real event to a transient
    gateway error is worse than leaving one thin name unflagged."""
    volume_by_symbol: dict[str, float | None] = {}
    for event in backlog:
        if event.symbol not in volume_by_symbol:
            volume_by_symbol[event.symbol] = await check_liquidity(gateway, event.symbol)

    inconclusive: list[str] = [
        symbol for symbol, volume in volume_by_symbol.items() if volume is None
    ]

    to_exclude = [
        event
        for event in backlog
        if (volume := volume_by_symbol[event.symbol]) is not None
        and volume < MIN_AVG_DOLLAR_VOLUME
    ]

    if not dry_run:
        for event in to_exclude:
            reason = (
                "trailing 10-session avg dollar volume "
                f"${volume_by_symbol[event.symbol]:,.0f} is under the "
                f"${MIN_AVG_DOLLAR_VOLUME:,.0f} research floor"
            )
            await conn.execute(
                """
                UPDATE earnings_events
                   SET liquidity_excluded_at = now(), liquidity_excluded_reason = $3
                 WHERE symbol = $1 AND earnings_date = $2
                """,
                event.symbol,
                event.earnings_date,
                reason,
            )

    return BackfillOutcome(
        excluded=tuple(to_exclude),
        kept_thin_check_inconclusive=tuple(sorted(inconclusive)),
        checked_symbols=len(volume_by_symbol),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check liquidity and report what would be excluded without writing anything",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            async with try_advisory_lock(conn, BACKFILL_LOCK_KEY) as acquired:
                if not acquired:
                    print("another backfill-liquidity run holds the lock; skipping")
                    return 0
                backlog = await fetch_insufficient_data_backlog(conn)
                if not backlog:
                    print("no unflagged insufficient_data events in the backlog")
                    return 0
                async with build_gateway_client(AppSettings()) as gateway:
                    outcome = await backfill_liquidity(
                        conn, gateway, backlog, dry_run=args.dry_run
                    )
    finally:
        await pool.close()

    verb = "would exclude" if args.dry_run else "excluded"
    excluded_symbols = sorted({event.symbol for event in outcome.excluded})
    print(
        f"checked {outcome.checked_symbols} distinct symbol(s) across {len(backlog)} "
        f"backlog event(s); {verb} {len(outcome.excluded)} event(s) "
        f"across {len(excluded_symbols)} symbol(s): {excluded_symbols}"
    )
    if outcome.kept_thin_check_inconclusive:
        print(
            "kept (liquidity check inconclusive): "
            f"{list(outcome.kept_thin_check_inconclusive)}"
        )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

"""CLI: fetch after-close earnings for a date range, record them in earnings_events,
and rewrite the persisted watchlist to the current active window."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import structlog

from afterhours_lab import calendar_reconcile, notify
from afterhours_lab.config import AppSettings
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.earnings import (
    EarningsCalendarClient,
    EarningsCalendarError,
    EarningsEntry,
    EarningsSettings,
    after_close_entries,
)
from afterhours_lab.gateway import BoundedGatewayClient, build_gateway_client
from afterhours_lab.status import status_path_for, write_status
from afterhours_lab.trading_calendar import next_trading_day, previous_trading_day
from afterhours_lab.watchlist import DEFAULT_WATCHLIST_PATH, save_watchlist

log = structlog.get_logger()

# The capture window around an earnings event: the trading day before, the earnings
# day itself, and the trading day after. A symbol stays "active" (watchable) while
# today falls anywhere in that window for its earnings_date.
#
# _previous_trading_day/_next_trading_day are re-exported from trading_calendar
# (rather than imported and used only under new names) so existing call sites —
# including tests that reach into archive_earnings._previous_trading_day — keep
# working unchanged.
_previous_trading_day = previous_trading_day
_next_trading_day = next_trading_day

# Re-entrancy guard: a second fixed advisory-lock key, distinct from db/migrate.py's
# ADVISORY_LOCK_KEY, so a stuck run (e.g. blocked in gateway backoff, since this app
# is registered priority: background) can't race a fresh cron fire on
# _upsert_earnings_events / save_watchlist. Non-blocking (pg_try_advisory_lock): if a
# previous run still holds it, this run skips rather than queuing behind a possibly
# wedged process.
ARCHIVE_LOCK_KEY = 0x41484C4541524E53  # the 8 ASCII bytes of "AHLEARNS" as an int64

# Liquidity floor: earnings whose trailing average daily dollar volume falls under
# this never enter earnings_events or the watchlist. These are names that don't get
# traded here, and tracking them just adds untradeable noise to the reaction
# statistics and the capture workload for no benefit.
LIQUIDITY_LOOKBACK_DAYS = 10
MIN_AVG_DOLLAR_VOLUME = 20_000_000.0


async def check_liquidity(gateway: BoundedGatewayClient, symbol: str) -> float | None:
    """Trailing average daily dollar volume for ``symbol``, or ``None`` if the
    history fetch failed or returned no bars (an inconclusive check, not a floor
    breach — see ``_filter_by_liquidity`` and ``backfill_liquidity``)."""
    try:
        response = await gateway.get_history(
            symbol, frequency="daily", days_back=LIQUIDITY_LOOKBACK_DAYS
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("liquidity_check_failed", symbol=symbol, error=str(exc))
        return None
    bars = response.history.bars
    if not bars:
        return None
    return sum(bar.close * bar.volume for bar in bars) / len(bars)


async def _filter_by_liquidity(
    gateway: BoundedGatewayClient, entries: list[EarningsEntry]
) -> tuple[list[EarningsEntry], list[str]]:
    """Drop entries whose trailing average daily dollar volume is under
    MIN_AVG_DOLLAR_VOLUME. Returns (kept, dropped_symbols).

    A symbol is kept, not dropped, when its history fetch fails or returns no bars:
    losing a real earnings event to a transient gateway error is worse than
    occasionally tracking one thin name.
    """
    kept: list[EarningsEntry] = []
    dropped: list[str] = []
    for entry in entries:
        avg_dollar_volume = await check_liquidity(gateway, entry.symbol)
        if avg_dollar_volume is None or avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME:
            kept.append(entry)
        else:
            dropped.append(entry.symbol)
    return kept, dropped


def parse_args(argv: list[str], today: dt.date) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive after-close earnings into earnings_events and refresh the watchlist"
    )
    # Default to the same window `_active_symbols` selects on — previous through next
    # trading day — not just `today`. Two reasons, both load-bearing:
    #   * The day_before capture fires at 3:55 PM for symbols whose earnings_date is
    #     the *next* trading day, so tomorrow's after-close names have to already be
    #     in earnings_events by this morning's run. Fetching only `today` would mean
    #     they never land in time and every day_before window came up empty.
    #   * Refetching the previous day backfills eps_actual/revenue_actual for prints
    #     that hadn't reported yet when they were first recorded (see the COALESCE
    #     directions in _upsert_earnings_events).
    parser.add_argument(
        "--from",
        dest="from_date",
        type=dt.date.fromisoformat,
        default=_previous_trading_day(today),
    )
    parser.add_argument(
        "--to", dest="to_date", type=dt.date.fromisoformat, default=_next_trading_day(today)
    )
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


async def _upsert_earnings_events(conn, entries: list[EarningsEntry]) -> int:
    """Upsert each entry; returns the count that were genuinely newly inserted (as
    opposed to an existing row being updated). Every call always writes a row (the
    ON CONFLICT below is DO UPDATE, never DO NOTHING), so that count is not
    len(entries) whenever a rerun or same-day refetch is filling in actuals for
    already-recorded symbols.

    Uses the standard Postgres upsert-detection idiom, `RETURNING (xmax = 0)`: xmax
    is the deleting-transaction id on a row version, which is unset (0) exactly on
    the version an INSERT just created, and set on a version an ON CONFLICT DO
    UPDATE rewrote. Issued as a per-row fetchrow loop rather than one batched
    executemany, since RETURNING needs a result per row and this batch is tiny (a
    handful of symbols/day).

    `hour` is deliberately insert-only (never in the UPDATE SET below): an
    already-recorded event's scheduling window must not be disturbed by a re-run.

    The calendar gets fetched both before a print (estimates populated, actuals
    null) and again after (actuals now populated), so a plain DO NOTHING would
    mean actuals from a later run never get saved. Actuals prefer the *new*
    incoming value when present, else keep what's stored, so a later run's real
    actuals fill in. Estimates/quarter/year prefer whatever is *already stored*
    when present, else take the new value, so they're stable once set.
    """
    if not entries:
        return 0
    inserted_count = 0
    for entry in entries:
        row = await conn.fetchrow(
            """
            INSERT INTO earnings_events (
                symbol, earnings_date, hour,
                eps_estimate, eps_actual, revenue_estimate, revenue_actual, quarter, year
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (symbol, earnings_date) DO UPDATE SET
                eps_estimate = COALESCE(earnings_events.eps_estimate, EXCLUDED.eps_estimate),
                eps_actual = COALESCE(EXCLUDED.eps_actual, earnings_events.eps_actual),
                revenue_estimate =
                    COALESCE(earnings_events.revenue_estimate, EXCLUDED.revenue_estimate),
                revenue_actual =
                    COALESCE(EXCLUDED.revenue_actual, earnings_events.revenue_actual),
                quarter = COALESCE(earnings_events.quarter, EXCLUDED.quarter),
                year = COALESCE(earnings_events.year, EXCLUDED.year)
            RETURNING (xmax = 0) AS inserted
            """,
            entry.symbol,
            entry.date,
            entry.hour,
            entry.eps_estimate,
            entry.eps_actual,
            entry.revenue_estimate,
            entry.revenue_actual,
            entry.quarter,
            entry.year,
        )
        if row["inserted"]:
            inserted_count += 1
    return inserted_count


async def _active_symbols(conn, today: dt.date) -> list[str]:
    window_start = _previous_trading_day(today)
    window_end = _next_trading_day(today)
    rows = await conn.fetch(
        """
        SELECT DISTINCT symbol FROM earnings_events
        WHERE earnings_date BETWEEN $1 AND $2
          AND liquidity_excluded_at IS NULL AND superseded_at IS NULL
        ORDER BY symbol
        """,
        window_start,
        window_end,
    )
    return [row["symbol"] for row in rows]


async def _run_locked(
    pool: DatabasePool,
    today: dt.date,
    matched: list[EarningsEntry],
    *,
    calendar_entries: list[EarningsEntry],
    window: tuple[dt.date, dt.date],
    lookup: calendar_reconcile.Lookup,
) -> tuple[list[str], int, calendar_reconcile.ReconcilePlan] | None:
    """Do the DB read/write under a non-blocking advisory lock, held on one connection
    for every operation (Postgres advisory locks are session-scoped, so a single
    connection has to span them). Returns None if another run already holds the lock,
    else (active_symbols, count of genuinely newly-inserted earnings_events rows, the
    calendar reconciliation applied).

    Reconciliation sees every fetched calendar entry, not just the liquid after-close
    ones archived here: a quarter re-dated to a thin day or to pre-market is still
    evidence that the recorded date is no longer the print."""
    async with pool.acquire() as conn:
        async with try_advisory_lock(conn, ARCHIVE_LOCK_KEY) as acquired:
            if not acquired:
                return None
            inserted_count = await _upsert_earnings_events(conn, matched)
            plan = await calendar_reconcile.reconcile(
                conn, calendar_entries, window=window, lookup=lookup
            )
            active = await _active_symbols(conn, today)
            return active, inserted_count, plan


def _record_failure(status_path: Path, detail: str) -> None:
    try:
        write_status(status_path, ok=False, detail=detail)
    except OSError as exc:
        log.error("status_write_failed", error=str(exc))
    # notify.send() returns None when Telegram isn't configured (expected, not a
    # problem) vs False for a genuine delivery failure — only the latter is worth a
    # warning; last_run_status.json above is already the failure record either way.
    if notify.send(f"AfterHoursLab archive-earnings FAILED: {detail}") is False:
        log.warning("notify_send_failed")


def _record_success(status_path: Path, detail: str) -> None:
    try:
        write_status(status_path, ok=True, detail=detail)
    except OSError as exc:
        log.error("status_write_failed", error=str(exc))


async def _main(argv: list[str], *, today: dt.date | None = None) -> int:
    today = today if today is not None else dt.date.today()
    args = parse_args(argv, today)
    status_path = status_path_for(args.watchlist)

    settings = EarningsSettings()
    async with EarningsCalendarClient(settings) as earnings:
        return await _archive(args, today, status_path, earnings)


async def _archive(
    args: argparse.Namespace, today: dt.date, status_path: Path, earnings: EarningsCalendarClient
) -> int:
    try:
        calendar_entries = await earnings.get_earnings_calendar(args.from_date, args.to_date)
    except EarningsCalendarError as exc:
        log.error("earnings_calendar_fetch_failed", error=str(exc))
        if not args.dry_run:
            _record_failure(status_path, f"earnings calendar fetch failed: {exc}")
        return 1

    async def lookup(symbol: str, from_date: dt.date, to_date: dt.date) -> list[EarningsEntry]:
        return await earnings.get_earnings_calendar(from_date, to_date, symbol=symbol)

    matched = after_close_entries(calendar_entries)
    dropped: list[str] = []
    if matched:
        async with build_gateway_client(AppSettings()) as gateway:
            matched, dropped = await _filter_by_liquidity(gateway, matched)
    if dropped:
        print(f"dropped for thin liquidity (<${MIN_AVG_DOLLAR_VOLUME:,.0f}/day avg): {dropped}")
    if matched:
        symbols = [entry.symbol for entry in matched]
        print(f"after-close earnings ({args.from_date}..{args.to_date}): {symbols}")
    else:
        print(f"no after-close earnings found for {args.from_date}..{args.to_date}")

    if args.dry_run:
        return 0

    # Everything past this point is the failure mode that bit us on 2026-08-20: a
    # PermissionError writing watchlist.json crashed silently with only the cron log
    # to show for it. Any exception here now also lands in last_run_status.json and,
    # if configured, a Telegram alert, before re-raising (so the log still gets the
    # full traceback — this doesn't swallow the failure, just stops it being silent).
    try:
        db_settings = DatabaseSettings()
        pool = await DatabasePool.connect(db_settings)
        try:
            result = await _run_locked(
                pool,
                today,
                matched,
                calendar_entries=calendar_entries,
                window=(args.from_date, args.to_date),
                lookup=lookup,
            )
        finally:
            await pool.close()

        if result is None:
            msg = "archive-earnings already running; skipped this run"
            print(msg)
            _record_success(status_path, msg)
            return 0

        active, inserted_count, plan = result
        for line in calendar_reconcile.describe(plan):
            print(line)
        # Stamped with the run's own `today`, not the file's mtime: that's what makes
        # a later reader able to tell a current watchlist from one left behind by a
        # run that stopped happening days ago.
        save_watchlist(active, args.watchlist, as_of=today)
        print(f"watchlist ({args.watchlist}): {active}")
        _record_success(
            status_path,
            f"archived {inserted_count} new event(s), {len(active)} active symbol(s)",
        )
        return 0
    except Exception as exc:
        _record_failure(status_path, f"unexpected error: {exc}")
        raise


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

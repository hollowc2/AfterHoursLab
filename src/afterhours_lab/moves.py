"""CLI: report the realized price move around each earnings event that has both its
day_before and day_after candles captured.

This only needs data already recorded by afterhours-lab-capture — no options/gateway
chain data involved. It's the "actual" half of an eventual expected-vs-actual move
comparison once implied-vol/expected-move data exists; until then it stands alone as
a report on what earnings prints actually did to the stock.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import sys

import structlog
from rich.console import Console
from rich.table import Table

from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool

log = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class MoveResult:
    """One earnings event's realized move, derived from its captured candles.

    `pre_close` is the last day_before candle's close (the price going into the
    print). `ah_close` is the last after_hours candle's close, if that window was
    captured — the immediate reaction, before the next full session can react to it
    too. `post_open`/`post_close` are the day_after window's first open and last
    close. Any of these can be None if the corresponding window has no candle rows
    even though its `*_captured` flag is set (e.g. a zero-quote poll window) — moves
    involving a None input are left as None rather than guessed at.
    """

    symbol: str
    earnings_date: dt.date
    eps_estimate: float | None
    eps_actual: float | None
    pre_close: float | None
    ah_close: float | None
    post_open: float | None
    post_close: float | None

    @property
    def reaction_move_pct(self) -> float | None:
        """pre_close -> ah_close: the immediate after-hours reaction to the print."""
        return _pct_change(self.pre_close, self.ah_close)

    @property
    def gap_move_pct(self) -> float | None:
        """pre_close -> post_open: the overnight gap into the next session's open."""
        return _pct_change(self.pre_close, self.post_open)

    @property
    def total_move_pct(self) -> float | None:
        """pre_close -> post_close: the full round-trip move through the next close."""
        return _pct_change(self.pre_close, self.post_close)

    @property
    def eps_surprise_pct(self) -> float | None:
        return _pct_change(self.eps_estimate, self.eps_actual)


def _pct_change(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before * 100.0


async def fetch_moves(
    conn, *, from_date: dt.date | None = None, to_date: dt.date | None = None
) -> list[MoveResult]:
    """Every earnings event with both its day_before and day_after windows captured,
    most recent first. Each candle lookup is a correlated LATERAL subquery rather than
    one aggregate query, so a missing window (no candle rows despite the captured
    flag) comes back as a clean None per-field instead of skewing an aggregate."""
    rows = await conn.fetch(
        """
        SELECT
            e.symbol, e.earnings_date, e.eps_estimate, e.eps_actual,
            pre.close AS pre_close,
            ah.close AS ah_close,
            post_first.open AS post_open,
            post_last.close AS post_close
        FROM earnings_events e
        LEFT JOIN LATERAL (
            SELECT close FROM candles c
            WHERE c.symbol = e.symbol AND c.earnings_date = e.earnings_date
              AND c.capture_window = 'day_before'
            ORDER BY c.ts DESC LIMIT 1
        ) pre ON true
        LEFT JOIN LATERAL (
            SELECT close FROM candles c
            WHERE c.symbol = e.symbol AND c.earnings_date = e.earnings_date
              AND c.capture_window = 'after_hours'
            ORDER BY c.ts DESC LIMIT 1
        ) ah ON true
        LEFT JOIN LATERAL (
            SELECT open FROM candles c
            WHERE c.symbol = e.symbol AND c.earnings_date = e.earnings_date
              AND c.capture_window = 'day_after'
            ORDER BY c.ts ASC LIMIT 1
        ) post_first ON true
        LEFT JOIN LATERAL (
            SELECT close FROM candles c
            WHERE c.symbol = e.symbol AND c.earnings_date = e.earnings_date
              AND c.capture_window = 'day_after'
            ORDER BY c.ts DESC LIMIT 1
        ) post_last ON true
        WHERE e.day_before_captured AND e.day_after_captured
          AND ($1::date IS NULL OR e.earnings_date >= $1)
          AND ($2::date IS NULL OR e.earnings_date <= $2)
        ORDER BY e.earnings_date DESC, e.symbol
        """,
        from_date,
        to_date,
    )
    return [
        MoveResult(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            eps_estimate=row["eps_estimate"],
            eps_actual=row["eps_actual"],
            pre_close=row["pre_close"],
            ah_close=row["ah_close"],
            post_open=row["post_open"],
            post_close=row["post_close"],
        )
        for row in rows
    ]


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def render_table(results: list[MoveResult]) -> Table:
    table = Table(title="AfterHoursLab — realized earnings moves")
    for column in (
        "Symbol",
        "Earnings Date",
        "EPS Surprise",
        "Reaction (AH)",
        "Gap (Open)",
        "Total Move",
    ):
        table.add_column(column)
    for result in results:
        table.add_row(
            result.symbol,
            result.earnings_date.isoformat(),
            _fmt_pct(result.eps_surprise_pct),
            _fmt_pct(result.reaction_move_pct),
            _fmt_pct(result.gap_move_pct),
            _fmt_pct(result.total_move_pct),
        )
    return table


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report realized price moves for captured earnings events"
    )
    parser.add_argument("--from", dest="from_date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--to", dest="to_date", type=dt.date.fromisoformat, default=None)
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)

    db_settings = DatabaseSettings()
    pool = await DatabasePool.connect(db_settings)
    try:
        async with pool.acquire() as conn:
            results = await fetch_moves(conn, from_date=args.from_date, to_date=args.to_date)
    finally:
        await pool.close()

    if not results:
        print("no fully-captured earnings events found")
        return 0

    Console().print(render_table(results))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

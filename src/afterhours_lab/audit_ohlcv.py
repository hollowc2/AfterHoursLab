"""Human-readable coverage audit for earnings OHLCV evidence."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from afterhours_lab.capture_ohlcv import PHASES
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit earnings OHLCV coverage")
    parser.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("symbols", nargs="*")
    return parser.parse_args(argv)


async def audit(conn, earnings_date: dt.date, symbols: list[str]) -> list[dict]:
    requested = [symbol.strip().upper() for symbol in symbols]
    rows = await conn.fetch(
        """
        SELECT e.symbol, c.phase, c.market_date, c.session, c.expected_start,
               c.expected_end, c.observed_first, c.observed_last,
               c.observed_minutes, c.expected_minutes, c.data_quality_flags,
               c.gateway_received_at, c.response_sha256, c.calendar,
               c.calendar_version, c.collection_mode
        FROM earnings_events e
        LEFT JOIN earnings_ohlcv_coverage c
          ON c.symbol=e.symbol AND c.earnings_date=e.earnings_date
        WHERE e.earnings_date=$1 AND e.hour=$2
          AND (cardinality($3::text[]) = 0 OR e.symbol=ANY($3::text[]))
        ORDER BY e.symbol, c.phase
        """,
        earnings_date,
        "amc",
        requested,
    )
    return [dict(row) for row in rows]


def render(rows: list[dict]) -> str:
    by_symbol: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], {})
        if row["phase"] is not None:
            by_symbol[row["symbol"]][row["phase"]] = row
    lines = [
        "symbol\tphase\tobserved/expected\tfirst\tlast\tmode\tcalendar\tsha256\tflags"
    ]
    for symbol, phases in by_symbol.items():
        for phase in PHASES:
            row = phases.get(phase)
            if row is None:
                lines.append(f"{symbol}\t{phase}\tMISSING\t-\t-\t-\t-\t-\t-")
                continue
            flags = ",".join(row["data_quality_flags"]) or "-"
            lines.append(
                f"{symbol}\t{phase}\t{row['observed_minutes']}/{row['expected_minutes']}\t"
                f"{row['observed_first'] or '-'}\t{row['observed_last'] or '-'}\t"
                f"{row['collection_mode']}\t"
                f"{row['calendar']}@{row['calendar_version']}\t"
                f"{row['response_sha256']}\t{flags}"
            )
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            rows = await audit(conn, args.date, args.symbols)
    finally:
        await pool.close()
    print(render(rows))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

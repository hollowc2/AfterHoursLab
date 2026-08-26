"""One-shot preservation of minute or extended-session gateway history."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from afterhours_lab.config import AppSettings
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.evidence import preserve_minute_history, preserve_session_history
from afterhours_lab.gateway import BoundedGatewayClient, build_gateway_client

MAX_SYMBOLS = 100


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preserve read-only gateway minute-history evidence"
    )
    parser.add_argument("symbols", nargs="+", help="equity symbols to collect")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        help="dated extended-session history (YYYY-MM-DD)",
    )
    source.add_argument(
        "--days-back",
        type=int,
        help="trailing minute history window",
    )
    args = parser.parse_args(argv)
    args.symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols))
    if any(not symbol for symbol in args.symbols):
        parser.error("symbols must not be blank")
    if len(args.symbols) > MAX_SYMBOLS:
        parser.error(f"at most {MAX_SYMBOLS} symbols may be collected per run")
    if args.days_back is not None and not 1 <= args.days_back <= 20:
        parser.error("--days-back must be between 1 and 20")
    return args


async def collect(
    gateway: BoundedGatewayClient,
    conn,
    symbols: list[str],
    *,
    date: dt.date | None,
    days_back: int | None,
) -> int:
    if date is not None:
        responses = await asyncio.gather(
            *(
                gateway.get_session_history(symbol, date, session="extended")
                for symbol in symbols
            )
        )
        for response in responses:
            await preserve_session_history(conn, response)
    else:
        responses = await asyncio.gather(
            *(
                gateway.get_history(symbol, frequency="minute", days_back=days_back)
                for symbol in symbols
            )
        )
        for response in responses:
            await preserve_minute_history(conn, response)
    return len(responses)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with build_gateway_client(AppSettings()) as gateway:
            async with pool.acquire() as conn:
                count = await collect(
                    gateway,
                    conn,
                    args.symbols,
                    date=args.date,
                    days_back=args.days_back,
                )
    finally:
        await pool.close()
    print(f"preserved gateway history for {count} symbol(s)")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

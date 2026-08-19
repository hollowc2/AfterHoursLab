"""Live quote viewer: polls the gateway on an interval and renders a refreshing table."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog
from rich.live import Live
from rich.table import Table
from schwab_gateway_sdk.client import GatewayClientError, GatewayMarketDataClient
from schwab_gateway_sdk.models import QuoteV1

from afterhours_lab.config import AppSettings
from afterhours_lab.gateway import build_gateway_client
from afterhours_lab.watchlist import (
    DEFAULT_WATCHLIST_PATH,
    add_symbol,
    load_watchlist,
    remove_symbol,
)

log = structlog.get_logger()

DEFAULT_INTERVAL_SECONDS = 5.0
BACKOFF_SECONDS = 15.0


def format_price(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def quote_to_row(quote: QuoteV1) -> tuple[str, ...]:
    return (
        quote.symbol,
        format_price(quote.last),
        format_price(quote.bid),
        format_price(quote.ask),
        format_price(quote.mark),
        str(quote.volume) if quote.volume is not None else "-",
        "stale" if quote.stale else "live",
        ",".join(quote.data_quality_flags) or "-",
    )


def render_table(rows: list[tuple[str, ...]], *, message: str | None = None) -> Table:
    table = Table(title="AfterHoursLab — watchlist")
    for column in ("Symbol", "Last", "Bid", "Ask", "Mark", "Volume", "State", "Flags"):
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    if message:
        table.caption = message
    return table


async def run_watch(
    gateway: GatewayMarketDataClient,
    symbols: list[str],
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    with Live(render_table([]), refresh_per_second=4) as live:
        while True:
            try:
                response = await gateway.get_quotes(symbols)
            except GatewayClientError as exc:
                log.error("watch_poll_failed", error=str(exc))
                live.update(render_table([], message=f"gateway error: {exc}"))
                await asyncio.sleep(BACKOFF_SECONDS)
                continue

            rows = [quote_to_row(quote) for quote in response.quotes]
            live.update(render_table(rows))
            await asyncio.sleep(interval_seconds)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live gateway quote viewer")
    parser.add_argument("symbols", nargs="*", help="symbols to watch for this run only")
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help="path to the persisted watchlist file",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--add", metavar="SYMBOL", help="add a symbol to the persisted watchlist")
    parser.add_argument(
        "--remove", metavar="SYMBOL", help="remove a symbol from the persisted watchlist"
    )
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.add:
        symbols = add_symbol(args.add, args.watchlist)
        print(f"watchlist ({args.watchlist}): {symbols}")
        return 0
    if args.remove:
        symbols = remove_symbol(args.remove, args.watchlist)
        print(f"watchlist ({args.watchlist}): {symbols}")
        return 0

    symbols = args.symbols or load_watchlist(args.watchlist)
    if not symbols:
        print(f"no symbols given and {args.watchlist} is empty; pass symbols or use --add")
        return 1

    settings = AppSettings()
    async with build_gateway_client(settings) as gateway:
        await run_watch(gateway, symbols, interval_seconds=args.interval)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

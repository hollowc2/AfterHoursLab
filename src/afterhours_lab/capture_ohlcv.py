"""Capture exact dated OHLCV phases around after-close earnings events."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import sys
from zoneinfo import ZoneInfo

from schwab_gateway_sdk.models import SessionHistoryResponseV1

from afterhours_lab.config import AppSettings
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.evidence import preserve_session_history
from afterhours_lab.gateway import BoundedGatewayClient, build_gateway_client
from afterhours_lab.trading_calendar import (
    CALENDAR_NAME,
    CALENDAR_VERSION,
    is_trading_day,
    previous_trading_day,
    regular_session_bounds,
)

EASTERN = ZoneInfo("America/New_York")

# Distinct from migrations, earnings archiving, and the legacy quote collector.
OHLCV_CAPTURE_LOCK_KEY = 0x41484C4F484C4356  # "AHLOHLCV"


@dataclasses.dataclass(frozen=True)
class Phase:
    session: str
    start: dt.time
    end: dt.time
    follows_earnings: bool = False


PHASES = {
    "earnings_regular": Phase("regular", dt.time(9, 30), dt.time(16, 0)),
    "earnings_postmarket": Phase("extended", dt.time(16, 0), dt.time(20, 0)),
    "following_premarket": Phase("extended", dt.time(4, 0), dt.time(9, 30), follows_earnings=True),
    "following_regular": Phase("regular", dt.time(9, 30), dt.time(16, 0), follows_earnings=True),
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture dated earnings OHLCV evidence")
    parser.add_argument("--phase", action="append", choices=sorted(PHASES), required=True)
    parser.add_argument("--market-date", type=dt.date.fromisoformat, default=dt.date.today())
    return parser.parse_args(argv)


def phase_dates(phase: Phase, market_date: dt.date) -> tuple[dt.date, dt.date]:
    earnings_date = previous_trading_day(market_date) if phase.follows_earnings else market_date
    return earnings_date, market_date


def phase_bounds(phase: Phase, market_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    market_open, market_close = regular_session_bounds(market_date)
    if phase.session == "regular":
        return market_open, market_close
    if phase.follows_earnings:
        return dt.datetime.combine(market_date, phase.start, EASTERN), market_open
    return market_close, dt.datetime.combine(market_date, phase.end, EASTERN)


async def _symbols(conn, earnings_date: dt.date) -> list[str]:
    rows = await conn.fetch(
        "SELECT symbol FROM earnings_events WHERE earnings_date=$1 AND hour=$2 ORDER BY symbol",
        earnings_date,
        "amc",
    )
    return [row["symbol"] for row in rows]


async def _record_coverage(
    conn,
    response: SessionHistoryResponseV1,
    *,
    phase_name: str,
    earnings_date: dt.date,
    market_date: dt.date,
) -> None:
    phase = PHASES[phase_name]
    start, end = phase_bounds(phase, market_date)
    candles = tuple(bar for bar in response.session_history.candles if start <= bar.timestamp < end)
    timestamps = sorted({bar.timestamp for bar in candles})
    flags = list(response.session_history.data_quality_flags)
    if not candles and "no_bars_returned" not in flags:
        flags.append("no_bars_in_requested_phase")
    if len(timestamps) != len(candles) and "duplicate_minute_bars" not in flags:
        flags.append("duplicate_minute_bars")
    await conn.execute(
        """
        INSERT INTO earnings_ohlcv_coverage (
            symbol, earnings_date, phase, market_date, session,
            expected_start, expected_end, observed_first, observed_last,
            observed_minutes, expected_minutes, source, gateway_received_at,
            response_sha256, data_quality_flags, calendar, calendar_version
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        ON CONFLICT (symbol, earnings_date, phase) DO UPDATE SET
            market_date=EXCLUDED.market_date, session=EXCLUDED.session,
            expected_start=EXCLUDED.expected_start, expected_end=EXCLUDED.expected_end,
            observed_first=EXCLUDED.observed_first, observed_last=EXCLUDED.observed_last,
            observed_minutes=EXCLUDED.observed_minutes,
            expected_minutes=EXCLUDED.expected_minutes, source=EXCLUDED.source,
            gateway_received_at=EXCLUDED.gateway_received_at,
            response_sha256=EXCLUDED.response_sha256,
            data_quality_flags=EXCLUDED.data_quality_flags,
            calendar=EXCLUDED.calendar, calendar_version=EXCLUDED.calendar_version,
            retrieved_at=now()
        """,
        response.session_history.symbol,
        earnings_date,
        phase_name,
        market_date,
        phase.session,
        start,
        end,
        timestamps[0] if timestamps else None,
        timestamps[-1] if timestamps else None,
        len(timestamps),
        int((end - start).total_seconds() // 60),
        response.session_history.source,
        response.session_history.gateway_received_at,
        hashlib.sha256(response.model_dump_json().encode()).hexdigest(),
        flags,
        CALENDAR_NAME,
        CALENDAR_VERSION,
    )


async def capture_phase(
    gateway: BoundedGatewayClient, conn, phase_name: str, market_date: dt.date
) -> int:
    phase = PHASES[phase_name]
    earnings_date, evidence_date = phase_dates(phase, market_date)
    symbols = await _symbols(conn, earnings_date)
    responses = await asyncio.gather(
        *(gateway.get_session_history(s, evidence_date, session=phase.session) for s in symbols)
    )
    for response in responses:
        await preserve_session_history(conn, response)
        await _record_coverage(
            conn,
            response,
            phase_name=phase_name,
            earnings_date=earnings_date,
            market_date=evidence_date,
        )
    return len(responses)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not is_trading_day(args.market_date):
        print(f"{args.market_date} is not an {CALENDAR_NAME} session; skipped")
        return 0
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with build_gateway_client(AppSettings()) as gateway:
            async with pool.acquire() as conn:
                async with try_advisory_lock(conn, OHLCV_CAPTURE_LOCK_KEY) as acquired:
                    if not acquired:
                        print("OHLCV capture already running; skipped")
                        return 0
                    for phase_name in args.phase:
                        count = await capture_phase(gateway, conn, phase_name, args.market_date)
                        print(f"{phase_name}: preserved {count} symbol(s)")
    finally:
        await pool.close()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

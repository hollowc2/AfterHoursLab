"""Typed reads for following-session evidence and persisted outcomes."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Mapping, Sequence
from typing import Any

from afterhours_lab.outcomes import FollowingSessionEvidence, OutcomeBar, OutcomeCoverage
from afterhours_lab.trading_calendar import next_trading_day, regular_session_bounds


def _coverage(row: Mapping[str, Any], bars: Sequence[Mapping[str, Any]]) -> OutcomeCoverage:
    return OutcomeCoverage(
        phase=row["phase"],
        market_date=row["market_date"],
        session=row["session"],
        expected_start=row["expected_start"],
        expected_end=row["expected_end"],
        observed_minutes=row["observed_minutes"],
        expected_minutes=row["expected_minutes"],
        source=row["source"],
        gateway_received_at=row["gateway_received_at"],
        response_sha256=row["response_sha256"],
        collection_mode=row["collection_mode"],
        calendar=row["calendar"],
        calendar_version=row["calendar_version"],
        data_quality_flags=tuple(row["data_quality_flags"] or ()),
        bars=tuple(
            OutcomeBar(
                ts=bar["ts"],
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=int(bar["volume"]) if bar["volume"] is not None else None,
            )
            for bar in bars
        ),
    )


async def fetch_following_session_evidence(
    conn, *, from_date: dt.date, to_date: dt.date, symbols: Sequence[str] = ()
) -> tuple[FollowingSessionEvidence, ...]:
    """Select bars only through each phase's exact authoritative coverage identity."""
    rows = await conn.fetch(
        """
        SELECT e.symbol, e.earnings_date, c.*
        FROM earnings_events e
        LEFT JOIN earnings_ohlcv_coverage c
          ON c.symbol=e.symbol AND c.earnings_date=e.earnings_date
         AND c.phase=ANY($4::text[])
        WHERE e.hour='amc' AND e.earnings_date BETWEEN $1 AND $2
          AND (cardinality($3::text[])=0 OR e.symbol=ANY($3::text[]))
        ORDER BY e.earnings_date, e.symbol, c.phase
        """,
        from_date,
        to_date,
        list(symbols),
        ["earnings_regular", "following_premarket", "following_regular"],
    )
    grouped: dict[tuple[str, dt.date], dict[str, OutcomeCoverage]] = {}
    for row in rows:
        key = (row["symbol"], row["earnings_date"])
        grouped.setdefault(key, {})
        if row["phase"] is None:
            continue
        bars = await conn.fetch(
            """
            SELECT ts, open, high, low, close, volume
            FROM bar_evidence
            WHERE symbol=$1 AND session=$2 AND evidence_date=$3
              AND gateway_endpoint='/v1/session-history'
              AND gateway_received_at=$4 AND source=$5
              AND ts >= $6 AND ts < $7
            ORDER BY ts, id
            """,
            row["symbol"],
            row["session"],
            row["market_date"],
            row["gateway_received_at"],
            row["source"],
            row["expected_start"],
            row["expected_end"],
        )
        grouped[key][row["phase"]] = _coverage(row, bars)
    result = []
    for (symbol, earnings_date), phases in sorted(grouped.items(), key=lambda item: item[0][::-1]):
        following_date = next_trading_day(earnings_date)
        _, following_close = regular_session_bounds(following_date)
        result.append(
            FollowingSessionEvidence(
                symbol=symbol,
                earnings_date=earnings_date,
                expected_following_close=following_close,
                regular=phases.get("earnings_regular"),
                premarket=phases.get("following_premarket"),
                following_regular=phases.get("following_regular"),
            )
        )
    return tuple(result)


@dataclasses.dataclass(frozen=True)
class FollowingSessionOutcome:
    symbol: str
    earnings_date: dt.date
    outcome_version: str
    analysis_status: str
    analysis_status_reason: str | None
    following_session_close_return: float | None
    source_evidence_sha256: str
    missing_fields: tuple[str, ...]
    data_quality_flags: tuple[str, ...]
    coverage_identities: dict[str, Any]
    values: dict[str, Any]


async def fetch_following_session_outcomes(
    conn, *, from_date: dt.date, to_date: dt.date, outcome_version: str
) -> tuple[FollowingSessionOutcome, ...]:
    rows = await conn.fetch(
        """SELECT * FROM following_session_outcomes
           WHERE earnings_date BETWEEN $1 AND $2 AND outcome_version=$3
           ORDER BY earnings_date, symbol,
                    CASE analysis_status WHEN 'complete' THEN 0 ELSE 1 END,
                    computed_at DESC, source_evidence_sha256""",
        from_date,
        to_date,
        outcome_version,
    )
    return tuple(
        FollowingSessionOutcome(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            outcome_version=row["outcome_version"],
            analysis_status=row["analysis_status"],
            analysis_status_reason=row["analysis_status_reason"],
            following_session_close_return=row["following_session_close_return"],
            source_evidence_sha256=row["source_evidence_sha256"],
            missing_fields=tuple(row["missing_fields"] or ()),
            data_quality_flags=tuple(row["data_quality_flags"] or ()),
            coverage_identities=(
                json.loads(row["coverage_identities"])
                if isinstance(row["coverage_identities"], str)
                else dict(row["coverage_identities"])
            ),
            values=json.loads(row["outcome_values"])
            if isinstance(row["outcome_values"], str)
            else dict(row["outcome_values"]),
        )
        for row in rows
    )

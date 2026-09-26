"""Insert-only persistence for following-session outcomes; never calls the gateway."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from afterhours_lab.canonical import canonical_json
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.outcomes import (
    OUTCOME_VERSION,
    FollowingSessionEvidence,
    compute_following_session_outcome,
)
from afterhours_lab.research.outcomes import fetch_following_session_evidence

ADVISORY_LOCK_KEY = 0x41464F5554434F4D  # "AFOUTCOM"
MAX_RANGE_DAYS = 370
INSERT_BATCH_SIZE = 100

VALUE_COLUMNS = (
    "reference_close",
    "reference_close_ts",
    "premarket_first",
    "premarket_high",
    "premarket_low",
    "premarket_last",
    "regular_open",
    "regular_high",
    "regular_low",
    "regular_close",
    "premarket_first_return",
    "premarket_high_return",
    "premarket_low_return",
    "premarket_last_return",
    "regular_open_return",
    "regular_high_return",
    "regular_low_return",
    "regular_close_return",
    "regular_return_5m",
    "regular_return_30m",
    "regular_return_60m",
    "overnight_gap_return",
    "following_session_close_return",
)
COUNT_COLUMNS = (
    "regular_observed_minutes",
    "regular_expected_minutes",
    "regular_coverage_ratio",
    "premarket_observed_minutes",
    "premarket_expected_minutes",
    "premarket_coverage_ratio",
    "following_regular_observed_minutes",
    "following_regular_expected_minutes",
    "following_regular_coverage_ratio",
)
COVERAGE_COLUMNS = (
    "earnings_regular_response_sha256",
    "earnings_regular_collection_mode",
    "following_premarket_response_sha256",
    "following_premarket_collection_mode",
    "following_regular_response_sha256",
    "following_regular_collection_mode",
    "coverage_identities",
)
INSERT_COLUMNS = (
    "symbol",
    "earnings_date",
    "outcome_version",
    "algorithm_name",
    "analysis_status",
    "analysis_status_reason",
    *VALUE_COLUMNS,
    *COUNT_COLUMNS,
    *COVERAGE_COLUMNS,
    "source_evidence_sha256",
    "parameters",
    "outcome_values",
    "missing_fields",
    "data_quality_flags",
    "computed_at",
)
def _insert_sql(row_count: int) -> str:
    if row_count < 1 or row_count > INSERT_BATCH_SIZE:
        raise ValueError(f"insert batch must contain 1..{INSERT_BATCH_SIZE} rows")
    width = len(INSERT_COLUMNS)
    groups = []
    for row_index in range(row_count):
        start = row_index * width + 1
        groups.append("(" + ", ".join(f"${index}" for index in range(start, start + width)) + ")")
    return f"""
INSERT INTO following_session_outcomes ({", ".join(INSERT_COLUMNS)})
VALUES {", ".join(groups)}
ON CONFLICT (symbol, earnings_date, outcome_version, source_evidence_sha256)
DO NOTHING RETURNING symbol
"""


INSERT_SQL = _insert_sql(1)


def build_outcome_row(evidence: FollowingSessionEvidence, *, now: dt.datetime) -> dict[str, Any]:
    row = compute_following_session_outcome(evidence, now=now)
    coverages = row.pop("coverages")
    for prefix, coverage in zip(
        ("earnings_regular", "following_premarket", "following_regular"),
        coverages,
        strict=True,
    ):
        row[f"{prefix}_response_sha256"] = coverage.response_sha256 if coverage else None
        row[f"{prefix}_collection_mode"] = coverage.collection_mode if coverage else None
    row["coverage_identities"] = {
        prefix: (
            {
                "phase": coverage.phase,
                "market_date": coverage.market_date,
                "session": coverage.session,
                "expected_start": coverage.expected_start,
                "expected_end": coverage.expected_end,
                "source": coverage.source,
                "gateway_received_at": coverage.gateway_received_at,
                "response_sha256": coverage.response_sha256,
                "collection_mode": coverage.collection_mode,
                "calendar": coverage.calendar,
                "calendar_version": coverage.calendar_version,
            }
            if coverage
            else None
        )
        for prefix, coverage in zip(
            ("earnings_regular", "following_premarket", "following_regular"),
            coverages,
            strict=True,
        )
    }
    values = {name: row[name] for name in VALUE_COLUMNS}
    # Observed premarket bar times have no column of their own; keep them with the
    # values they qualify.
    values["premarket_first_ts"] = row["premarket_first_ts"]
    values["premarket_last_ts"] = row["premarket_last_ts"]
    row["outcome_values"] = values
    row["computed_at"] = now
    return row


@dataclasses.dataclass(frozen=True)
class OutcomePersistSummary:
    scanned: int
    complete: int
    insufficient: int
    not_yet_available: int
    inserted: int
    conflicted: int
    rows: tuple[dict[str, Any], ...]


async def persist_outcomes(
    conn,
    evidence: Sequence[FollowingSessionEvidence],
    *,
    dry_run: bool = False,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> OutcomePersistSummary:
    now = clock()
    rows = tuple(build_outcome_row(item, now=now) for item in evidence)
    persisted = tuple(row for row in rows if row["analysis_status"] != "not_yet_available")
    inserted = 0
    if not dry_run:
        async with conn.transaction():
            for start in range(0, len(persisted), INSERT_BATCH_SIZE):
                batch = persisted[start : start + INSERT_BATCH_SIZE]
                values: list[Any] = []
                for row in batch:
                    for column in INSERT_COLUMNS:
                        value = row[column]
                        if column in {"parameters", "outcome_values", "coverage_identities"}:
                            value = canonical_json(value)
                        values.append(value)
                inserted += len(await conn.fetch(_insert_sql(len(batch)), *values))
    return OutcomePersistSummary(
        scanned=len(rows),
        complete=sum(row["analysis_status"] == "complete" for row in rows),
        insufficient=sum(row["analysis_status"] == "insufficient_data" for row in rows),
        not_yet_available=sum(row["analysis_status"] == "not_yet_available" for row in rows),
        inserted=inserted,
        conflicted=0 if dry_run else len(persisted) - inserted,
        rows=rows,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist versioned following-session outcomes")
    parser.add_argument("--from", dest="from_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must be on or before --to")
    if (args.to_date - args.from_date).days + 1 > MAX_RANGE_DAYS:
        parser.error(f"date range must not exceed {MAX_RANGE_DAYS} days")
    from afterhours_lab.research.filters import normalize_symbol

    try:
        args.symbol = list(dict.fromkeys(normalize_symbol(symbol) for symbol in args.symbol))
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _jsonable(summary: OutcomePersistSummary) -> dict[str, Any]:
    return dataclasses.asdict(summary)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            async with try_advisory_lock(conn, ADVISORY_LOCK_KEY) as acquired:
                if not acquired:
                    print("another persist-outcomes run holds the lock; skipping")
                    return 0
                evidence = await fetch_following_session_evidence(
                    conn, from_date=args.from_date, to_date=args.to_date, symbols=args.symbol
                )
                summary = await persist_outcomes(conn, evidence, dry_run=args.dry_run)
    finally:
        await pool.close()
    if args.json:
        print(json.dumps(_jsonable(summary), default=str, sort_keys=True))
    else:
        prefix = "dry run; " if args.dry_run else ""
        print(
            f"{prefix}scanned={summary.scanned} complete={summary.complete} "
            f"insufficient={summary.insufficient} not_yet_available={summary.not_yet_available} "
            f"inserted={summary.inserted} conflicted={summary.conflicted} "
            f"version={OUTCOME_VERSION}"
        )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

"""Recompute earnings-reaction features that were computed before their coverage
existed, now that it does.

``persist_reactions`` is deliberately insert-only: a row's *values* never change once
written, so a study can cite them safely (see its module docstring). But
``analysis_status = 'insufficient_data'`` with reason "missing authoritative regular
or postmarket coverage" means the row was computed from *no* evidence at all — a
later ``historical_backfill`` run can fill that coverage in without the original
persist_reactions run ever finding out, since it never re-queries a version triple it
already has a row for. That row is then permanently stuck reporting "no data" even
after the data exists.

This tool never changes a stored row's values either. For each event whose live row
still says "missing authoritative regular or postmarket coverage", it recomputes
against current evidence and compares the new evidence digest to the stored one:

* Unchanged digest (nothing new arrived) — leave the row alone.
* Changed digest — stamp the old row ``retracted_at``/``retracted_reason`` (metadata
  only) and insert a fresh row under the same version triple with the recomputed
  result. Both rows remain in the table permanently; the retracted one is still
  queryable directly for audit, just excluded from every research view (see
  ``earnings_reaction_features_live_key`` in migration 012).

A recomputed row can itself turn out ``insufficient_data`` for a different, still-true
reason (e.g. the backfill only partially covered the postmarket session) — that's not
a failure of this tool, just a more accurate stale reason than the original.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import sys
from collections.abc import Sequence

from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.persist_reactions import (
    INSERT_COLUMNS,
    INSERT_SQL,
    build_row,
    evidence_digest,
)
from afterhours_lab.reactions import (
    CLASSIFIER_VERSION,
    DETECTOR_VERSION,
    EventEvidence,
    compute_reaction_features,
    fetch_event_evidence,
)
from afterhours_lab.research.filters import FEATURE_VERSION

STALE_REASON = "missing authoritative regular or postmarket coverage"

# Distinct from every other advisory-lock key in this codebase: the 8 ASCII bytes of
# "AHLRPRCS" as an int64.
REPROCESS_LOCK_KEY = 0x41484C5250524353


@dataclasses.dataclass(frozen=True)
class StaleRow:
    symbol: str
    earnings_date: dt.date
    source_evidence_sha256: str


@dataclasses.dataclass(frozen=True)
class ReprocessOutcome:
    reprocessed: tuple[tuple[str, dt.date, str], ...]  # (symbol, earnings_date, new_status)
    unchanged: tuple[tuple[str, dt.date], ...]  # evidence digest identical; left alone


async def fetch_stale_missing_coverage_rows(
    conn,
    *,
    feature_version: str = FEATURE_VERSION,
    detector_version: str = DETECTOR_VERSION,
    classifier_version: str = CLASSIFIER_VERSION,
) -> list[StaleRow]:
    rows = await conn.fetch(
        """
        SELECT symbol, earnings_date, source_evidence_sha256
        FROM earnings_reaction_features
        WHERE feature_version = $1 AND detector_version = $2 AND classifier_version = $3
          AND retracted_at IS NULL
          AND analysis_status = 'insufficient_data'
          AND analysis_status_reason = $4
        ORDER BY symbol, earnings_date
        """,
        feature_version,
        detector_version,
        classifier_version,
        STALE_REASON,
    )
    return [
        StaleRow(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            source_evidence_sha256=row["source_evidence_sha256"],
        )
        for row in rows
    ]


async def reprocess_stale_coverage(
    conn,
    stale_rows: Sequence[StaleRow],
    events_by_key: dict[tuple[str, dt.date], EventEvidence],
    *,
    feature_version: str = FEATURE_VERSION,
    detector_version: str = DETECTOR_VERSION,
    classifier_version: str = CLASSIFIER_VERSION,
    dry_run: bool = False,
) -> ReprocessOutcome:
    reprocessed: list[tuple[str, dt.date, str]] = []
    unchanged: list[tuple[str, dt.date]] = []

    for stale in stale_rows:
        key = (stale.symbol, stale.earnings_date)
        event = events_by_key.get(key)
        if event is None:
            continue
        new_digest = evidence_digest(event)
        if new_digest == stale.source_evidence_sha256:
            unchanged.append(key)
            continue

        row = build_row(event, compute_reaction_features(event))
        if not dry_run:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE earnings_reaction_features
                       SET retracted_at = now(), retracted_reason = $5
                     WHERE symbol = $1 AND earnings_date = $2
                       AND feature_version = $3 AND detector_version = $4
                       AND retracted_at IS NULL
                    """,
                    stale.symbol,
                    stale.earnings_date,
                    feature_version,
                    detector_version,
                    (
                        "superseded by reprocess-stale-coverage: evidence changed "
                        f"(old digest {stale.source_evidence_sha256[:12]}..., "
                        f"new digest {new_digest[:12]}...)"
                    ),
                )
                values = [row[column] for column in INSERT_COLUMNS]
                await conn.fetchval(INSERT_SQL, *values)
        reprocessed.append((stale.symbol, stale.earnings_date, row["analysis_status"]))

    return ReprocessOutcome(reprocessed=tuple(reprocessed), unchanged=tuple(unchanged))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="recompute and report without retracting or inserting anything",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            async with try_advisory_lock(conn, REPROCESS_LOCK_KEY) as acquired:
                if not acquired:
                    print("another reprocess-stale-coverage run holds the lock; skipping")
                    return 0
                stale_rows = await fetch_stale_missing_coverage_rows(conn)
                if not stale_rows:
                    print("no live rows still report missing authoritative coverage")
                    return 0
                dates = [row.earnings_date for row in stale_rows]
                symbols = sorted({row.symbol for row in stale_rows})
                events = await fetch_event_evidence(
                    conn, from_date=min(dates), to_date=max(dates), symbols=symbols
                )
                events_by_key = {
                    (event.symbol, event.earnings_date): event for event in events
                }
                outcome = await reprocess_stale_coverage(
                    conn, stale_rows, events_by_key, dry_run=args.dry_run
                )
    finally:
        await pool.close()

    verb = "would reprocess" if args.dry_run else "reprocessed"
    print(
        f"checked {len(stale_rows)} stale row(s); {verb} {len(outcome.reprocessed)}, "
        f"left {len(outcome.unchanged)} unchanged (evidence still identical)"
    )
    for symbol, earnings_date, new_status in outcome.reprocessed:
        print(f"  {symbol} {earnings_date}: now {new_status}")
    if outcome.unchanged:
        print(
            "unchanged: "
            + ", ".join(f"{symbol} {earnings_date}" for symbol, earnings_date in outcome.unchanged)
        )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

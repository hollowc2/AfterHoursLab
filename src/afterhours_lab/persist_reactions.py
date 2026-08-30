"""Persist computed earnings-reaction features into ``earnings_reaction_features``.

``reactions.py`` deliberately computes and reports without writing.  Cross-sectional
research needs the same numbers queryable, so this module is the one place that turns
a ``ReactionFeatures`` into a stored row.

Two properties make a stored row trustworthy:

* **Insert-only.**  Every write is ``ON CONFLICT DO NOTHING`` on the version triple, so
  a rerun can never silently change a value another study already cited.  Changing a
  definition means bumping a version and inserting alongside the old generation.
* **Self-describing.**  ``source_evidence_sha256`` digests the exact bars and coverage
  identities the row was computed from, so a later rerun can prove the inputs were the
  same evidence rather than merely the same symbol and date.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from typing import Any

import structlog

from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.reactions import (
    CLASSIFIER_VERSION,
    DELAYED_AFTER_MINUTES,
    DETECTOR_VERSION,
    HORIZONS_MINUTES,
    MIN_POSTMARKET_BARS,
    REACTION_THRESHOLD_PCT,
    EventEvidence,
    PathClassification,
    ReactionFeatures,
    compute_reaction_features,
    fetch_event_evidence,
)
from afterhours_lab.research.filters import ALGORITHM_NAME, FEATURE_VERSION

log = structlog.get_logger()

# Unique to this writer so a persistence run and a capture run never block each other.
ADVISORY_LOCK_KEY = 0x414652454143544E  # the 8 ASCII bytes of "AFREACTN"

STUDY_WINDOW_MINUTES = max(HORIZONS_MINUTES)

STATUS_BY_CLASSIFICATION = {
    PathClassification.CONTINUATION: "complete",
    PathClassification.FADE: "complete",
    PathClassification.DELAYED: "complete",
    PathClassification.WHIPSAW: "complete",
    PathClassification.NO_MEANINGFUL: "no_trigger_in_window",
    PathClassification.INSUFFICIENT_DATA: "insufficient_data",
}

# The numeric columns the table's CHECK constraint counts. Any that is NULL on an
# insufficient_data row is reported in missing_fields.
NUMERIC_FIELDS = (
    "reference_price",
    "initial_return",
    "return_1m",
    "return_5m",
    "return_15m",
    "return_30m",
    "return_60m",
    "return_105m",
    "window_high_return",
    "window_low_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "max_retracement",
    "reaction_vwap_proxy",
    "volume_1m",
    "volume_5m",
    "volume_15m",
    "volume_30m",
    "volume_60m",
    "volume_105m",
)

INSERT_SQL = """
INSERT INTO earnings_reaction_features (
    symbol, earnings_date, feature_version, algorithm_name,
    detector_version, classifier_version,
    analysis_status, analysis_status_reason, reaction_class, reaction_direction,
    feature_window_start, feature_window_end, feature_cutoff_ts, label_cutoff_ts,
    reaction_timestamp, detection_delay_minutes,
    reference_price, initial_return,
    return_1m, return_5m, return_15m, return_30m, return_60m, return_105m,
    window_high_return, window_low_return,
    max_favorable_excursion, max_adverse_excursion, max_retracement,
    reaction_vwap_proxy,
    volume_1m, volume_5m, volume_15m, volume_30m, volume_60m, volume_105m,
    source_bar_count, study_observed_minutes, study_missing_minutes,
    study_coverage_ratio,
    earnings_regular_response_sha256, earnings_regular_collection_mode,
    earnings_postmarket_response_sha256, earnings_postmarket_collection_mode,
    source_evidence_sha256, parameters, missing_fields, data_quality_flags
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10,
    $11, $12, $13, $14,
    $15, $16,
    $17, $18,
    $19, $20, $21, $22, $23, $24,
    $25, $26,
    $27, $28, $29,
    $30,
    $31, $32, $33, $34, $35, $36,
    $37, $38, $39, $40,
    $41, $42, $43, $44,
    $45, $46::jsonb, $47::text[], $48::text[]
)
ON CONFLICT (symbol, earnings_date, feature_version, detector_version, classifier_version)
DO NOTHING
RETURNING symbol
"""

INSERT_COLUMNS = (
    "symbol",
    "earnings_date",
    "feature_version",
    "algorithm_name",
    "detector_version",
    "classifier_version",
    "analysis_status",
    "analysis_status_reason",
    "reaction_class",
    "reaction_direction",
    "feature_window_start",
    "feature_window_end",
    "feature_cutoff_ts",
    "label_cutoff_ts",
    "reaction_timestamp",
    "detection_delay_minutes",
    "reference_price",
    "initial_return",
    "return_1m",
    "return_5m",
    "return_15m",
    "return_30m",
    "return_60m",
    "return_105m",
    "window_high_return",
    "window_low_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "max_retracement",
    "reaction_vwap_proxy",
    "volume_1m",
    "volume_5m",
    "volume_15m",
    "volume_30m",
    "volume_60m",
    "volume_105m",
    "source_bar_count",
    "study_observed_minutes",
    "study_missing_minutes",
    "study_coverage_ratio",
    "earnings_regular_response_sha256",
    "earnings_regular_collection_mode",
    "earnings_postmarket_response_sha256",
    "earnings_postmarket_collection_mode",
    "source_evidence_sha256",
    "parameters",
    "missing_fields",
    "data_quality_flags",
)


def algorithm_parameters() -> dict[str, Any]:
    """The definition a stored row was produced under, recorded with the row."""
    return {
        "reaction_threshold_pct": REACTION_THRESHOLD_PCT,
        "delayed_after_minutes": DELAYED_AFTER_MINUTES,
        "min_postmarket_bars": MIN_POSTMARKET_BARS,
        "horizons_minutes": list(HORIZONS_MINUTES),
        "study_window_minutes": STUDY_WINDOW_MINUTES,
        "fade_retracement_pct": 50.0,
        "signal_convention": "close_confirmed_at_interval_end",
        "reference_price": "final_regular_session_minute_close",
        "calendar": "XNYS",
    }


def evidence_digest(event: EventEvidence) -> str:
    """SHA-256 over the exact evidence a row was computed from.

    Includes the coverage response hashes (which retrieval was authoritative) *and*
    every bar value (what that retrieval actually contained), so the digest changes if
    either the chosen retrieval or its contents differ.
    """

    def bar_tuple(bar) -> list[Any]:
        return [bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]

    payload = {
        "symbol": event.symbol,
        "earnings_date": event.earnings_date.isoformat(),
        "regular_response_sha256": event.regular_response_sha256,
        "postmarket_response_sha256": event.postmarket_response_sha256,
        "regular_expected_start": _iso(event.regular_expected_start),
        "regular_expected_end": _iso(event.regular_expected_end),
        "postmarket_expected_start": _iso(event.postmarket_expected_start),
        "postmarket_expected_end": _iso(event.postmarket_expected_end),
        "regular_bars": sorted(
            (bar_tuple(bar) for bar in event.regular_bars), key=lambda item: item[0]
        ),
        "postmarket_bars": sorted(
            (bar_tuple(bar) for bar in event.postmarket_bars), key=lambda item: item[0]
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _study_window(event: EventEvidence) -> tuple[
    dt.datetime | None, dt.datetime | None, dt.datetime | None, dt.datetime | None
]:
    """The four window timestamps, or all-None when the phase bounds cannot carry them.

    The table requires ``label_cutoff_ts <= feature_window_end``; an early close whose
    postmarket phase ends before the 105-minute study window cannot satisfy that, and
    such an event is always ``insufficient_data`` anyway, where the columns are optional.
    """
    start, end = event.postmarket_expected_start, event.postmarket_expected_end
    if start is None or end is None:
        return None, None, None, None
    cutoff = start + dt.timedelta(minutes=STUDY_WINDOW_MINUTES)
    if cutoff > end or start >= end:
        return None, None, None, None
    return start, end, cutoff, cutoff


def build_row(event: EventEvidence, features: ReactionFeatures) -> dict[str, Any]:
    """Map a computed result onto the stored row. Pure, so it is testable without a DB."""
    status = STATUS_BY_CLASSIFICATION[features.classification]
    window_start, window_end, feature_cutoff, label_cutoff = _study_window(event)

    if status == "insufficient_data":
        reaction_class = None
        direction = None
    elif status == "no_trigger_in_window":
        reaction_class = PathClassification.NO_MEANINGFUL.value
        direction = "flat"
    else:
        reaction_class = features.classification.value
        direction = "up" if (features.initial_return_pct or 0.0) > 0 else "down"

    volumes = features.cumulative_volume
    returns = features.returns_pct
    row: dict[str, Any] = {
        "symbol": features.symbol,
        "earnings_date": features.earnings_date,
        "feature_version": FEATURE_VERSION,
        "algorithm_name": ALGORITHM_NAME,
        "detector_version": features.detector_version,
        "classifier_version": features.classifier_version,
        "analysis_status": status,
        "analysis_status_reason": features.reason,
        "reaction_class": reaction_class,
        "reaction_direction": direction,
        "feature_window_start": window_start,
        "feature_window_end": window_end,
        "feature_cutoff_ts": feature_cutoff,
        "label_cutoff_ts": label_cutoff,
        "reaction_timestamp": features.reaction_timestamp,
        "detection_delay_minutes": features.detection_delay_minutes,
        "reference_price": features.pre_close,
        "initial_return": features.initial_return_pct,
        "return_1m": returns.get(1),
        "return_5m": returns.get(5),
        "return_15m": returns.get(15),
        "return_30m": returns.get(30),
        "return_60m": returns.get(60),
        "return_105m": returns.get(105),
        "window_high_return": features.window_high_return_pct,
        "window_low_return": features.window_low_return_pct,
        "max_favorable_excursion": features.max_favorable_excursion_pct,
        "max_adverse_excursion": features.max_adverse_excursion_pct,
        "max_retracement": features.retracement_pct,
        "reaction_vwap_proxy": features.reaction_vwap_proxy,
        "volume_1m": volumes.get(1),
        "volume_5m": volumes.get(5),
        "volume_15m": volumes.get(15),
        "volume_30m": volumes.get(30),
        "volume_60m": volumes.get(60),
        "volume_105m": volumes.get(105),
        "source_bar_count": len(event.regular_bars) + len(event.postmarket_bars),
        "study_observed_minutes": features.study_observed_minutes,
        "study_missing_minutes": features.study_missing_minutes,
        "study_coverage_ratio": features.study_coverage_ratio,
        "earnings_regular_response_sha256": features.regular_response_sha256,
        "earnings_regular_collection_mode": features.regular_collection_mode,
        "earnings_postmarket_response_sha256": features.postmarket_response_sha256,
        "earnings_postmarket_collection_mode": features.postmarket_collection_mode,
        "source_evidence_sha256": evidence_digest(event),
        "parameters": json.dumps(algorithm_parameters(), sort_keys=True),
        "data_quality_flags": list(features.data_quality_flags),
    }
    row["missing_fields"] = (
        [name for name in NUMERIC_FIELDS if row[name] is None]
        if status == "insufficient_data"
        else []
    )
    return row


@dataclasses.dataclass(frozen=True)
class PersistOutcome:
    inserted: int
    already_present: int
    rows: tuple[dict[str, Any], ...]

    @property
    def considered(self) -> int:
        return self.inserted + self.already_present


async def persist_features(
    conn,
    events: Sequence[EventEvidence],
    *,
    dry_run: bool = False,
) -> PersistOutcome:
    """Insert one row per event, skipping any that already exists for this version."""
    rows = tuple(build_row(event, compute_reaction_features(event)) for event in events)
    if dry_run:
        return PersistOutcome(inserted=0, already_present=0, rows=rows)

    inserted = 0
    for row in rows:
        values = [row[column] for column in INSERT_COLUMNS]
        result = await conn.fetchval(INSERT_SQL, *values)
        if result is not None:
            inserted += 1
    return PersistOutcome(
        inserted=inserted,
        already_present=len(rows) - inserted,
        rows=rows,
    )


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dt.datetime | dt.date):
            payload[key] = value.isoformat()
        elif key == "parameters":
            payload[key] = json.loads(value)
        else:
            payload[key] = value
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute and insert earnings-reaction features. Insert-only: an existing "
            "row for the same version triple is never overwritten."
        )
    )
    parser.add_argument("--from", dest="from_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--symbol", action="append", default=[], help="repeat to limit symbols")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and report without writing anything",
    )
    parser.add_argument("--json", action="store_true", help="emit the mapped rows as JSON")
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must be on or before --to")
    normalized: list[str] = []
    for raw_symbol in args.symbol:
        symbol = raw_symbol.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
            parser.error(f"invalid equity symbol: {raw_symbol}")
        if symbol not in normalized:
            normalized.append(symbol)
    args.symbol = normalized
    return args


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            async with try_advisory_lock(conn, ADVISORY_LOCK_KEY) as acquired:
                if not acquired:
                    print("another persist-reactions run holds the lock; skipping")
                    return 0
                events = await fetch_event_evidence(
                    conn,
                    from_date=args.from_date,
                    to_date=args.to_date,
                    symbols=args.symbol,
                )
                outcome = await persist_features(conn, events, dry_run=args.dry_run)
    finally:
        await pool.close()

    if args.json:
        print(json.dumps([_jsonable(row) for row in outcome.rows], indent=2, sort_keys=True))
        return 0

    if args.dry_run:
        by_status: dict[str, int] = {}
        for row in outcome.rows:
            by_status[row["analysis_status"]] = by_status.get(row["analysis_status"], 0) + 1
        breakdown = ", ".join(f"{status}={count}" for status, count in sorted(by_status.items()))
        print(f"dry run: {len(outcome.rows)} event(s) computed, nothing written ({breakdown})")
        return 0

    print(
        f"inserted {outcome.inserted} new feature row(s); "
        f"{outcome.already_present} already present for "
        f"{FEATURE_VERSION}/{DETECTOR_VERSION}/{CLASSIFIER_VERSION}"
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

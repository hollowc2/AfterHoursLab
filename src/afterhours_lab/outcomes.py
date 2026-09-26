"""Pure following-session outcome engine (no database or gateway access)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Any

from afterhours_lab.canonical import canonical_digest

# v2 (2026-09-25): v1 required every minute of all three phases, which only
# mega-caps ever satisfy (9 of 79 eligible events complete). v2 keeps exact-bar
# labels but reads premarket from its first/last *observed* bars with no coverage
# floor, and needs MIN_REGULAR_COVERAGE_RATIO rather than 100% of each regular
# session. v1 rows remain for audit and for studies pinned to v1.
OUTCOME_VERSION = "following-session-v2"
OUTCOME_ALGORITHM = "following_session_observation"
HORIZONS_MINUTES = (5, 30, 60)
AUTHORITATIVE_CAPTURE_GRACE_MINUTES = 10
MIN_REGULAR_COVERAGE_RATIO = 0.95


@dataclasses.dataclass(frozen=True)
class OutcomeBar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None


@dataclasses.dataclass(frozen=True)
class OutcomeCoverage:
    phase: str
    market_date: dt.date
    session: str
    expected_start: dt.datetime
    expected_end: dt.datetime
    observed_minutes: int
    expected_minutes: int
    source: str
    gateway_received_at: dt.datetime
    response_sha256: str
    collection_mode: str
    calendar: str
    calendar_version: str
    data_quality_flags: tuple[str, ...]
    bars: tuple[OutcomeBar, ...]

    @property
    def coverage_ratio(self) -> float:
        return min(1.0, self.observed_minutes / self.expected_minutes)


@dataclasses.dataclass(frozen=True)
class FollowingSessionEvidence:
    symbol: str
    earnings_date: dt.date
    expected_following_close: dt.datetime
    regular: OutcomeCoverage | None
    premarket: OutcomeCoverage | None
    following_regular: OutcomeCoverage | None


def _return(reference: float | None, price: float | None) -> float | None:
    if reference is None or price is None:
        return None
    return (price - reference) / reference * 100.0


def _bars(coverage: OutcomeCoverage | None) -> tuple[OutcomeBar, ...]:
    if coverage is None:
        return ()
    by_ts: dict[dt.datetime, OutcomeBar] = {}
    for bar in sorted(coverage.bars, key=lambda item: item.ts):
        by_ts.setdefault(bar.ts, bar)
    return tuple(by_ts.values())


def source_evidence_digest(evidence: FollowingSessionEvidence) -> str:
    phases: list[dict[str, Any] | None] = []
    for coverage in (evidence.regular, evidence.premarket, evidence.following_regular):
        if coverage is None:
            phases.append(None)
            continue
        phases.append(
            {
                "phase": coverage.phase,
                "market_date": coverage.market_date,
                "session": coverage.session,
                "expected_start": coverage.expected_start,
                "expected_end": coverage.expected_end,
                "observed_minutes": coverage.observed_minutes,
                "expected_minutes": coverage.expected_minutes,
                "source": coverage.source,
                "gateway_received_at": coverage.gateway_received_at,
                "response_sha256": coverage.response_sha256,
                "collection_mode": coverage.collection_mode,
                "calendar": coverage.calendar,
                "calendar_version": coverage.calendar_version,
                "data_quality_flags": sorted(set(coverage.data_quality_flags)),
                "bars": [
                    dataclasses.asdict(bar)
                    for bar in sorted(
                        coverage.bars,
                        key=lambda item: (
                            item.ts,
                            item.open,
                            item.high,
                            item.low,
                            item.close,
                            item.volume if item.volume is not None else -1,
                        ),
                    )
                ],
            }
        )
    return canonical_digest(
        "following-session-source-evidence-v1",
        {"symbol": evidence.symbol, "earnings_date": evidence.earnings_date, "phases": phases},
    )


def compute_following_session_outcome(
    evidence: FollowingSessionEvidence, *, now: dt.datetime
) -> dict[str, Any]:
    """Compute labels from exact authoritative bars.

    Minute N is the close-confirmed bar whose start timestamp is session_open + N - 1
    minutes. A missing exact timestamp remains missing; no nearest-bar substitution occurs.
    Premarket is the exception: it trades sparsely, so its first/last values are the
    first and last bars actually observed in the authoritative capture (their
    timestamps are recorded), and at least one bar is required.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    premarket = _bars(evidence.premarket)
    following = _bars(evidence.following_regular)
    following_open = (
        evidence.following_regular.expected_start if evidence.following_regular else None
    )

    def exact(coverage: OutcomeCoverage | None, timestamp: dt.datetime | None) -> OutcomeBar | None:
        if coverage is None or timestamp is None:
            return None
        return next((bar for bar in _bars(coverage) if bar.ts == timestamp), None)

    reference_bar = exact(
        evidence.regular,
        evidence.regular.expected_end - dt.timedelta(minutes=1) if evidence.regular else None,
    )
    reference = reference_bar.close if reference_bar else None
    horizon_bars = {
        minute: next(
            (
                bar
                for bar in following
                if following_open is not None
                and bar.ts == following_open + dt.timedelta(minutes=minute - 1)
            ),
            None,
        )
        for minute in HORIZONS_MINUTES
    }

    premarket_first_bar = premarket[0] if premarket else None
    premarket_last_bar = premarket[-1] if premarket else None
    following_open_bar = exact(evidence.following_regular, following_open)
    following_close_bar = exact(
        evidence.following_regular,
        evidence.following_regular.expected_end - dt.timedelta(minutes=1)
        if evidence.following_regular
        else None,
    )
    values: dict[str, Any] = {
        "reference_close": reference,
        "reference_close_ts": reference_bar.ts if reference_bar else None,
        "premarket_first": premarket_first_bar.open if premarket_first_bar else None,
        "premarket_first_ts": premarket_first_bar.ts if premarket_first_bar else None,
        "premarket_high": max((bar.high for bar in premarket), default=None),
        "premarket_low": min((bar.low for bar in premarket), default=None),
        "premarket_last": premarket_last_bar.close if premarket_last_bar else None,
        "premarket_last_ts": premarket_last_bar.ts if premarket_last_bar else None,
        "regular_open": following_open_bar.open if following_open_bar else None,
        "regular_high": max((bar.high for bar in following), default=None),
        "regular_low": min((bar.low for bar in following), default=None),
        "regular_close": following_close_bar.close if following_close_bar else None,
    }
    for name in ("premarket_first", "premarket_high", "premarket_low", "premarket_last"):
        values[f"{name}_return"] = _return(reference, values[name])
    for name in ("regular_open", "regular_high", "regular_low", "regular_close"):
        values[f"{name}_return"] = _return(reference, values[name])
    for minute, bar in horizon_bars.items():
        values[f"regular_return_{minute}m"] = _return(reference, bar.close if bar else None)
    values["overnight_gap_return"] = values["regular_open_return"]
    values["following_session_close_return"] = values["regular_close_return"]

    required = (
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
        "regular_return_5m",
        "regular_return_30m",
        "regular_return_60m",
    )
    missing = [name for name in required if values[name] is None]
    missing_phases = [
        phase
        for phase, coverage in (
            ("earnings_regular", evidence.regular),
            ("following_premarket", evidence.premarket),
            ("following_regular", evidence.following_regular),
        )
        if coverage is None
    ]
    incomplete_phases = [
        f"{coverage.phase}_coverage"
        for coverage in (evidence.regular, evidence.following_regular)
        if coverage is not None and coverage.coverage_ratio < MIN_REGULAR_COVERAGE_RATIO
    ]
    invalid_phases = [
        f"{coverage.phase}_invalid_bar"
        for coverage in (evidence.regular, evidence.premarket, evidence.following_regular)
        if coverage is not None
        and any(
            not all(
                math.isfinite(value) and value > 0
                for value in (bar.open, bar.high, bar.low, bar.close)
            )
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
            or bar.high < bar.low
            for bar in coverage.bars
        )
    ]
    duplicate_phases = [
        f"{coverage.phase}_duplicate_timestamp"
        for coverage in (evidence.regular, evidence.premarket, evidence.following_regular)
        if coverage is not None and len({bar.ts for bar in coverage.bars}) != len(coverage.bars)
    ]
    missing.extend(incomplete_phases)
    missing.extend(invalid_phases)
    missing.extend(duplicate_phases)
    available_after = evidence.expected_following_close + dt.timedelta(
        minutes=AUTHORITATIVE_CAPTURE_GRACE_MINUTES
    )
    if now < available_after and (
        evidence.following_regular is None
        or now < evidence.following_regular.expected_end
        or evidence.following_regular.observed_minutes
        < evidence.following_regular.expected_minutes
    ):
        status = "not_yet_available"
        reason = "following exchange session or authoritative capture is not complete"
    elif missing:
        status = "insufficient_data"
        reason = "required authoritative evidence or exact close-confirmed horizon is missing"
    else:
        status = "complete"
        reason = None

    coverages = (evidence.regular, evidence.premarket, evidence.following_regular)
    flags = sorted(
        set(flag for coverage in coverages if coverage for flag in coverage.data_quality_flags)
        | set(invalid_phases)
        | set(duplicate_phases)
    )
    return {
        "symbol": evidence.symbol,
        "earnings_date": evidence.earnings_date,
        "outcome_version": OUTCOME_VERSION,
        "algorithm_name": OUTCOME_ALGORITHM,
        "analysis_status": status,
        "analysis_status_reason": reason,
        **values,
        "regular_observed_minutes": evidence.regular.observed_minutes if evidence.regular else 0,
        "regular_expected_minutes": evidence.regular.expected_minutes if evidence.regular else 390,
        "regular_coverage_ratio": evidence.regular.coverage_ratio if evidence.regular else 0.0,
        "premarket_observed_minutes": (
            evidence.premarket.observed_minutes if evidence.premarket else 0
        ),
        "premarket_expected_minutes": (
            evidence.premarket.expected_minutes if evidence.premarket else 150
        ),
        "premarket_coverage_ratio": (
            evidence.premarket.coverage_ratio if evidence.premarket else 0.0
        ),
        "following_regular_observed_minutes": (
            evidence.following_regular.observed_minutes if evidence.following_regular else 0
        ),
        "following_regular_expected_minutes": (
            evidence.following_regular.expected_minutes if evidence.following_regular else 390
        ),
        "following_regular_coverage_ratio": (
            evidence.following_regular.coverage_ratio if evidence.following_regular else 0.0
        ),
        "source_evidence_sha256": source_evidence_digest(evidence),
        "missing_fields": sorted(set(missing_phases + missing)),
        "data_quality_flags": flags,
        "parameters": {
            "calendar": "XNYS",
            "horizons_minutes": list(HORIZONS_MINUTES),
            "horizon_semantics": "close of minute N; bar start=session_open+N-1m",
            "reference": "last close in authoritative earnings_regular coverage",
            "premarket_semantics": "first/last observed bar in 07:00-09:30 ET coverage",
            "min_regular_coverage_ratio": MIN_REGULAR_COVERAGE_RATIO,
            "authoritative_capture_grace_minutes": AUTHORITATIVE_CAPTURE_GRACE_MINUTES,
            "available_after": available_after,
            "execution_assumptions": "none",
        },
        "coverages": coverages,
    }

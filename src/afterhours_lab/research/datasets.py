"""Typed research datasets shared by the CLI, notebooks, and the website.

The architectural rule this module exists to enforce: nothing outside it writes SQL
against the research tables, and nothing outside ``afterhours_lab.reactions``
recomputes a feature.  Every interface asks this layer for typed rows, so a change to
a join or a surprise definition lands in exactly one place.

All rows are plain frozen dataclasses with a ``to_record()`` mapping, which is what
``afterhours_lab.research.export`` turns into CSV, Pandas, Polars, or Parquet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from afterhours_lab.reactions import HORIZONS_MINUTES, REACTION_THRESHOLD_PCT
from afterhours_lab.research.filters import EventFilter, ParamBuilder, normalize_symbol

EASTERN = ZoneInfo("America/New_York")

PHASES = (
    "earnings_regular",
    "earnings_postmarket",
    "following_premarket",
    "following_regular",
)

# Phases the reaction study itself depends on. A missing one is a study-blocking gap,
# not merely a thinner chart.
STUDY_PHASES = ("earnings_regular", "earnings_postmarket")

STUDY_WINDOW_MINUTES = max(HORIZONS_MINUTES)


def _as_list(value: Any) -> list[str]:
    return list(value) if value else []


def _as_json_object(value: Any) -> dict[str, Any]:
    """asyncpg hands back JSONB as text unless a codec is registered."""
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


# --------------------------------------------------------------------------- rows


@dataclasses.dataclass(frozen=True)
class EventSummary:
    """One after-close earnings event joined to its persisted reaction features.

    ``analysis_status`` is ``None`` when no feature row exists for the requested
    version triple — the event is known but has not been analyzed under this
    definition, which is different from having been analyzed and found unusable.
    """

    symbol: str
    earnings_date: dt.date
    hour: str | None
    eps_estimate: float | None
    eps_actual: float | None
    revenue_estimate: float | None
    revenue_actual: float | None
    quarter: int | None
    year: int | None
    eps_surprise_pct: float | None
    revenue_surprise_pct: float | None
    feature_version: str | None
    detector_version: str | None
    classifier_version: str | None
    analysis_status: str | None
    analysis_status_reason: str | None
    reaction_class: str | None
    reaction_direction: str | None
    reaction_timestamp: dt.datetime | None
    detection_delay_minutes: int | None
    reference_price: float | None
    initial_return: float | None
    return_1m: float | None
    return_5m: float | None
    return_15m: float | None
    return_30m: float | None
    return_60m: float | None
    return_105m: float | None
    retention: float | None
    window_high_return: float | None
    window_low_return: float | None
    max_favorable_excursion: float | None
    max_adverse_excursion: float | None
    max_retracement: float | None
    reaction_vwap_proxy: float | None
    volume_105m: int | None
    study_observed_minutes: int | None
    study_missing_minutes: int | None
    study_coverage_ratio: float | None
    earnings_regular_collection_mode: str | None
    earnings_postmarket_collection_mode: str | None
    source_evidence_sha256: str | None
    data_quality_flags: tuple[str, ...]
    missing_fields: tuple[str, ...]
    computed_at: dt.datetime | None
    covered_phases: tuple[str, ...]
    note_count: int

    @property
    def analyzed(self) -> bool:
        return self.analysis_status is not None

    @property
    def missing_phases(self) -> tuple[str, ...]:
        return tuple(phase for phase in PHASES if phase not in self.covered_phases)

    @property
    def study_ready(self) -> bool:
        """Whether both phases the reaction study reads are covered."""
        return all(phase in self.covered_phases for phase in STUDY_PHASES)

    @property
    def any_historical_backfill(self) -> bool:
        return "historical_backfill" in {
            self.earnings_regular_collection_mode,
            self.earnings_postmarket_collection_mode,
        }

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["data_quality_flags"] = ";".join(self.data_quality_flags)
        record["missing_fields"] = ";".join(self.missing_fields)
        record["covered_phases"] = ";".join(self.covered_phases)
        record["missing_phases"] = ";".join(self.missing_phases)
        return record

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> EventSummary:
        return cls(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            hour=row["hour"],
            eps_estimate=row["eps_estimate"],
            eps_actual=row["eps_actual"],
            revenue_estimate=row["revenue_estimate"],
            revenue_actual=row["revenue_actual"],
            quarter=row["quarter"],
            year=row["year"],
            eps_surprise_pct=row["eps_surprise_pct"],
            revenue_surprise_pct=row["revenue_surprise_pct"],
            feature_version=row["feature_version"],
            detector_version=row["detector_version"],
            classifier_version=row["classifier_version"],
            analysis_status=row["analysis_status"],
            analysis_status_reason=row["analysis_status_reason"],
            reaction_class=row["reaction_class"],
            reaction_direction=row["reaction_direction"],
            reaction_timestamp=row["reaction_timestamp"],
            detection_delay_minutes=row["detection_delay_minutes"],
            reference_price=row["reference_price"],
            initial_return=row["initial_return"],
            return_1m=row["return_1m"],
            return_5m=row["return_5m"],
            return_15m=row["return_15m"],
            return_30m=row["return_30m"],
            return_60m=row["return_60m"],
            return_105m=row["return_105m"],
            retention=row["retention"],
            window_high_return=row["window_high_return"],
            window_low_return=row["window_low_return"],
            max_favorable_excursion=row["max_favorable_excursion"],
            max_adverse_excursion=row["max_adverse_excursion"],
            max_retracement=row["max_retracement"],
            reaction_vwap_proxy=row["reaction_vwap_proxy"],
            volume_105m=row["volume_105m"],
            study_observed_minutes=row["study_observed_minutes"],
            study_missing_minutes=row["study_missing_minutes"],
            study_coverage_ratio=row["study_coverage_ratio"],
            earnings_regular_collection_mode=row["earnings_regular_collection_mode"],
            earnings_postmarket_collection_mode=row["earnings_postmarket_collection_mode"],
            source_evidence_sha256=row["source_evidence_sha256"],
            data_quality_flags=tuple(_as_list(row["data_quality_flags"])),
            missing_fields=tuple(_as_list(row["missing_fields"])),
            computed_at=row["computed_at"],
            covered_phases=tuple(_as_list(row["covered_phases"])),
            note_count=row["note_count"] or 0,
        )


@dataclasses.dataclass(frozen=True)
class CohortResult:
    """Rows plus the disclosure of what the filter left out.

    ``universe_count`` is every after-close event in the requested date/symbol scope;
    ``included_count`` is how many survived the refinements.  An interface that shows
    ``rows`` without also showing these two numbers is hiding its own selection.
    """

    rows: tuple[EventSummary, ...]
    universe_count: int
    included_count: int
    excluded_by_status: tuple[tuple[str, int], ...]
    event_filter: EventFilter

    @property
    def excluded_count(self) -> int:
        return self.universe_count - self.included_count

    @property
    def page_count(self) -> int:
        return len(self.rows)

    @property
    def truncated(self) -> bool:
        return self.page_count < self.included_count

    def to_records(self) -> list[dict[str, Any]]:
        return [row.to_record() for row in self.rows]


@dataclasses.dataclass(frozen=True)
class CoveragePhase:
    """One ``earnings_ohlcv_coverage`` row: the auditable claim about a phase."""

    symbol: str
    earnings_date: dt.date
    phase: str
    market_date: dt.date
    session: str
    expected_start: dt.datetime
    expected_end: dt.datetime
    observed_first: dt.datetime | None
    observed_last: dt.datetime | None
    observed_minutes: int
    expected_minutes: int
    source: str
    gateway_received_at: dt.datetime
    response_sha256: str
    data_quality_flags: tuple[str, ...]
    collection_mode: str
    calendar: str
    calendar_version: str
    retrieved_at: dt.datetime

    @property
    def coverage_ratio(self) -> float:
        return min(1.0, self.observed_minutes / self.expected_minutes)

    @property
    def missing_minutes(self) -> int:
        return max(0, self.expected_minutes - self.observed_minutes)

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["data_quality_flags"] = ";".join(self.data_quality_flags)
        record["coverage_ratio"] = self.coverage_ratio
        record["missing_minutes"] = self.missing_minutes
        return record

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CoveragePhase:
        return cls(
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            phase=row["phase"],
            market_date=row["market_date"],
            session=row["session"],
            expected_start=row["expected_start"],
            expected_end=row["expected_end"],
            observed_first=row["observed_first"],
            observed_last=row["observed_last"],
            observed_minutes=row["observed_minutes"],
            expected_minutes=row["expected_minutes"],
            source=row["source"],
            gateway_received_at=row["gateway_received_at"],
            response_sha256=row["response_sha256"],
            data_quality_flags=tuple(_as_list(row["data_quality_flags"])),
            collection_mode=row["collection_mode"],
            calendar=row["calendar"],
            calendar_version=row["calendar_version"],
            retrieved_at=row["retrieved_at"],
        )


@dataclasses.dataclass(frozen=True)
class PathBar:
    """One minute of the event's price path, tagged with the phase it belongs to."""

    symbol: str
    earnings_date: dt.date
    phase: str
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["ts_eastern"] = self.ts.astimezone(EASTERN).isoformat()
        return record


@dataclasses.dataclass(frozen=True)
class ResearchNote:
    id: int
    symbol: str
    earnings_date: dt.date
    author: str
    body: str
    tags: tuple[str, ...]
    created_at: dt.datetime

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["tags"] = ";".join(self.tags)
        return record


@dataclasses.dataclass(frozen=True)
class EventDetail:
    """Everything one event page needs, assembled by a single call."""

    summary: EventSummary
    coverage: tuple[CoveragePhase, ...]
    bars: tuple[PathBar, ...]
    notes: tuple[ResearchNote, ...]
    parameters: dict[str, Any]

    def bars_for(self, phase: str) -> tuple[PathBar, ...]:
        return tuple(bar for bar in self.bars if bar.phase == phase)

    def coverage_for(self, phase: str) -> CoveragePhase | None:
        return next((row for row in self.coverage if row.phase == phase), None)

    @property
    def postmarket_start(self) -> dt.datetime | None:
        phase = self.coverage_for("earnings_postmarket")
        return phase.expected_start if phase else None

    @property
    def study_cutoff(self) -> dt.datetime | None:
        start = self.postmarket_start
        return start + dt.timedelta(minutes=STUDY_WINDOW_MINUTES) if start else None


@dataclasses.dataclass(frozen=True)
class TodayCandidate:
    """The 12:30-2:45 PM cockpit row for one of today's after-close reporters.

    Everything derived from live evidence is explicitly provisional: it is computed
    from whatever minutes happen to have been recorded so far, not from an audited
    coverage row.  ``finalized`` is true only once a persisted feature row exists,
    which is the only value safe to compare against historical research.
    """

    symbol: str
    earnings_date: dt.date
    eps_estimate: float | None
    eps_actual: float | None
    eps_surprise_pct: float | None
    pre_close_reference: float | None
    pre_close_source_ts: dt.datetime | None
    bid: float | None
    ask: float | None
    mark: float | None
    last: float | None
    quote_volume: int | None
    quote_received_at: dt.datetime | None
    quote_stale: bool | None
    quote_age_seconds: float | None
    quote_quality_flags: tuple[str, ...]
    provisional_move_pct: float | None
    provisional_last_bar_ts: dt.datetime | None
    provisional_bar_minutes: int
    first_threshold_cross_ts: dt.datetime | None
    first_threshold_cross_pct: float | None
    covered_phases: tuple[str, ...]
    finalized: bool
    finalized_class: str | None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        spread, mid = self.spread, self.mid
        if spread is None or not mid:
            return None
        return spread / mid * 100.0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def degradation_reasons(self) -> tuple[str, ...]:
        """Why today's numbers may not be trustworthy, in plain terms."""
        reasons: list[str] = []
        if self.quote_received_at is None:
            reasons.append("no quote evidence recorded for this event")
        if self.quote_stale:
            reasons.append("gateway reported the quote as stale")
        if self.quote_age_seconds is not None and self.quote_age_seconds > 60:
            reasons.append(f"quote age {self.quote_age_seconds:.0f}s")
        if self.pre_close_reference is None:
            reasons.append("no pre-close reference minute recorded")
        reasons.extend(self.quote_quality_flags)
        if not self.finalized:
            reasons.append("no finalized feature row yet")
        return tuple(dict.fromkeys(reasons))

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["quote_quality_flags"] = ";".join(self.quote_quality_flags)
        record["covered_phases"] = ";".join(self.covered_phases)
        record["spread"] = self.spread
        record["spread_pct"] = self.spread_pct
        return record


@dataclasses.dataclass(frozen=True)
class OperationsSnapshot:
    """Freshness of each writer, read from what it actually wrote."""

    last_earnings_event_created_at: dt.datetime | None
    last_coverage_retrieved_at: dt.datetime | None
    last_bar_received_at: dt.datetime | None
    last_quote_received_at: dt.datetime | None
    last_feature_computed_at: dt.datetime | None
    events_total: int
    events_amc: int
    coverage_rows: int
    scheduled_coverage_rows: int
    backfill_coverage_rows: int
    feature_rows: int

    def to_record(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class QualityIssue:
    """One reason an event is not research-grade, with the numbers behind it."""

    symbol: str
    earnings_date: dt.date
    kind: str
    detail: str
    phases_missing: tuple[str, ...]
    observed_minutes: int | None
    expected_minutes: int | None

    def to_record(self) -> dict[str, Any]:
        record = dataclasses.asdict(self)
        record["phases_missing"] = ";".join(self.phases_missing)
        return record


# ----------------------------------------------------------------------- queries

_EVENT_COLUMNS = """
        e.symbol,
        e.earnings_date,
        e.hour,
        e.eps_estimate,
        e.eps_actual,
        e.revenue_estimate,
        e.revenue_actual,
        e.quarter,
        e.year,
        CASE WHEN e.eps_estimate IS NOT NULL AND e.eps_estimate <> 0
             THEN (e.eps_actual - e.eps_estimate) / abs(e.eps_estimate) * 100.0
        END AS eps_surprise_pct,
        CASE WHEN e.revenue_estimate IS NOT NULL AND e.revenue_estimate <> 0
             THEN (e.revenue_actual - e.revenue_estimate)
                  / abs(e.revenue_estimate) * 100.0
        END AS revenue_surprise_pct,
        f.feature_version,
        f.detector_version,
        f.classifier_version,
        f.analysis_status,
        f.analysis_status_reason,
        f.reaction_class,
        f.reaction_direction,
        f.reaction_timestamp,
        f.detection_delay_minutes,
        f.reference_price,
        f.initial_return,
        f.return_1m,
        f.return_5m,
        f.return_15m,
        f.return_30m,
        f.return_60m,
        f.return_105m,
        CASE WHEN f.initial_return IS NOT NULL AND f.initial_return <> 0
             THEN f.return_105m / f.initial_return
        END AS retention,
        f.window_high_return,
        f.window_low_return,
        f.max_favorable_excursion,
        f.max_adverse_excursion,
        f.max_retracement,
        f.reaction_vwap_proxy,
        f.volume_105m,
        f.study_observed_minutes,
        f.study_missing_minutes,
        f.study_coverage_ratio,
        f.earnings_regular_collection_mode,
        f.earnings_postmarket_collection_mode,
        f.source_evidence_sha256,
        COALESCE(f.data_quality_flags, ARRAY[]::text[]) AS data_quality_flags,
        COALESCE(f.missing_fields, ARRAY[]::text[]) AS missing_fields,
        f.computed_at,
        COALESCE(cov.covered_phases, ARRAY[]::text[]) AS covered_phases,
        COALESCE(notes.note_count, 0) AS note_count
"""

_EVENT_JOINS = """
        FROM earnings_events e
        LEFT JOIN earnings_reaction_features f
               ON f.symbol = e.symbol
              AND f.earnings_date = e.earnings_date
              AND f.feature_version = {fv}
              AND f.detector_version = {dv}
              AND f.classifier_version = {cv}
        LEFT JOIN (
            SELECT symbol, earnings_date,
                   array_agg(phase ORDER BY phase) AS covered_phases
            FROM earnings_ohlcv_coverage
            GROUP BY symbol, earnings_date
        ) cov ON cov.symbol = e.symbol AND cov.earnings_date = e.earnings_date
        LEFT JOIN (
            SELECT symbol, earnings_date, count(*)::int AS note_count
            FROM research_notes
            GROUP BY symbol, earnings_date
        ) notes ON notes.symbol = e.symbol AND notes.earnings_date = e.earnings_date
"""


def _universe_cte(event_filter: EventFilter, params: ParamBuilder) -> str:
    joins = _EVENT_JOINS.format(
        fv=params.add(event_filter.versions.feature_version),
        dv=params.add(event_filter.versions.detector_version),
        cv=params.add(event_filter.versions.classifier_version),
    )
    return f"SELECT {_EVENT_COLUMNS} {joins} WHERE {event_filter.scope_sql(params)}"


async def fetch_cohort(conn, event_filter: EventFilter) -> CohortResult:
    """Load a filtered cohort together with the counts needed to disclose exclusions.

    One round trip builds the universe twice — once for the page of rows, once for
    the counts — because the counts must reflect the whole cohort, not the page.
    """
    params = ParamBuilder()
    universe = _universe_cte(event_filter, params)
    refinement = event_filter.refinement_sql(params)
    limit_sql = ""
    if event_filter.limit is not None:
        limit_sql = f"LIMIT {params.add(event_filter.limit)}"
    offset_sql = f"OFFSET {params.add(event_filter.offset)}" if event_filter.offset else ""

    sql = f"""
        WITH universe AS ({universe})
        SELECT * FROM universe
        WHERE {refinement}
        ORDER BY {event_filter.order_sql}
        {limit_sql} {offset_sql}
    """
    rows = await conn.fetch(sql, *params.params)

    count_params = ParamBuilder()
    count_universe = _universe_cte(event_filter, count_params)
    count_refinement = event_filter.refinement_sql(count_params)
    count_sql = f"""
        WITH universe AS ({count_universe}),
        matched AS (SELECT * FROM universe WHERE {count_refinement})
        SELECT
            (SELECT count(*)::int FROM universe) AS universe_count,
            (SELECT count(*)::int FROM matched) AS included_count,
            (
                SELECT COALESCE(jsonb_object_agg(status, total), '{{}}'::jsonb)
                FROM (
                    SELECT COALESCE(analysis_status, 'not_analyzed') AS status,
                           count(*)::int AS total
                    FROM universe u
                    WHERE NOT EXISTS (
                        SELECT 1 FROM matched m
                        WHERE m.symbol = u.symbol
                          AND m.earnings_date = u.earnings_date
                    )
                    GROUP BY 1
                ) excluded
            ) AS excluded_by_status
    """
    counts = await conn.fetchrow(count_sql, *count_params.params)

    excluded = tuple(
        sorted(
            (status, int(total))
            for status, total in _as_json_object(counts["excluded_by_status"]).items()
        )
    )
    return CohortResult(
        rows=tuple(EventSummary.from_row(row) for row in rows),
        universe_count=counts["universe_count"],
        included_count=counts["included_count"],
        excluded_by_status=excluded,
        event_filter=event_filter,
    )


async def fetch_event_summary(
    conn,
    symbol: str,
    earnings_date: dt.date,
    *,
    event_filter: EventFilter | None = None,
) -> EventSummary | None:
    """One event, read through the same joins the cohort query uses."""
    symbol = normalize_symbol(symbol)
    base = event_filter or EventFilter()
    scoped = dataclasses.replace(
        base,
        date_from=earnings_date,
        date_to=earnings_date,
        symbols=(symbol,),
        analysis_statuses=(),
        reaction_classes=(),
        directions=(),
        postmarket_collection_modes=(),
        min_abs_initial_return=None,
        max_abs_initial_return=None,
        min_retention=None,
        max_retention=None,
        min_detection_delay_minutes=None,
        max_detection_delay_minutes=None,
        min_volume_105m=None,
        min_coverage_ratio=None,
        min_eps_surprise_pct=None,
        max_eps_surprise_pct=None,
        limit=1,
        offset=0,
    )
    params = ParamBuilder()
    sql = f"SELECT * FROM ({_universe_cte(scoped, params)}) universe LIMIT 1"
    row = await conn.fetchrow(sql, *params.params)
    return EventSummary.from_row(row) if row else None


async def fetch_coverage(
    conn, symbol: str, earnings_date: dt.date
) -> tuple[CoveragePhase, ...]:
    rows = await conn.fetch(
        """
        SELECT * FROM earnings_ohlcv_coverage
        WHERE symbol = $1 AND earnings_date = $2
        ORDER BY expected_start
        """,
        normalize_symbol(symbol),
        earnings_date,
    )
    return tuple(CoveragePhase.from_row(row) for row in rows)


async def fetch_event_bars(
    conn,
    symbol: str,
    earnings_date: dt.date,
    coverage: Sequence[CoveragePhase] | None = None,
) -> tuple[PathBar, ...]:
    """Bars from exactly the retrieval each coverage row names.

    This mirrors ``reactions.fetch_event_evidence``: a chart must show the same
    evidence the features were computed from, or the picture and the number disagree.
    """
    symbol = normalize_symbol(symbol)
    phases = coverage if coverage is not None else await fetch_coverage(conn, symbol, earnings_date)
    bars: list[PathBar] = []
    for phase in phases:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (ts) ts, open, high, low, close, volume
            FROM bar_evidence
            WHERE symbol = $1 AND session = $2 AND evidence_date = $3
              AND gateway_endpoint = '/v1/session-history'
              AND gateway_received_at = $4 AND source = $5
              AND ts >= $6 AND ts < $7
            ORDER BY ts, id
            """,
            symbol,
            phase.session,
            phase.market_date,
            phase.gateway_received_at,
            phase.source,
            phase.expected_start,
            phase.expected_end,
        )
        bars.extend(
            PathBar(
                symbol=symbol,
                earnings_date=earnings_date,
                phase=phase.phase,
                ts=row["ts"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]) if row["volume"] is not None else None,
            )
            for row in rows
        )
    bars.sort(key=lambda bar: (bar.ts, bar.phase))
    return tuple(bars)


async def fetch_notes(
    conn, symbol: str, earnings_date: dt.date
) -> tuple[ResearchNote, ...]:
    rows = await conn.fetch(
        """
        SELECT id, symbol, earnings_date, author, body, tags, created_at
        FROM research_notes
        WHERE symbol = $1 AND earnings_date = $2
        ORDER BY created_at DESC, id DESC
        """,
        normalize_symbol(symbol),
        earnings_date,
    )
    return tuple(
        ResearchNote(
            id=row["id"],
            symbol=row["symbol"],
            earnings_date=row["earnings_date"],
            author=row["author"],
            body=row["body"],
            tags=tuple(_as_list(row["tags"])),
            created_at=row["created_at"],
        )
        for row in rows
    )


async def add_note(
    conn,
    symbol: str,
    earnings_date: dt.date,
    *,
    author: str,
    body: str,
    tags: Sequence[str] = (),
) -> ResearchNote:
    """Append a note. Notes are never edited — a correction is a later note."""
    author, body = author.strip(), body.strip()
    if not author:
        raise ValueError("author must not be blank")
    if not body:
        raise ValueError("note body must not be blank")
    cleaned = tuple(dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip()))
    row = await conn.fetchrow(
        """
        INSERT INTO research_notes (symbol, earnings_date, author, body, tags)
        VALUES ($1, $2, $3, $4, $5::text[])
        RETURNING id, symbol, earnings_date, author, body, tags, created_at
        """,
        normalize_symbol(symbol),
        earnings_date,
        author,
        body,
        list(cleaned),
    )
    return ResearchNote(
        id=row["id"],
        symbol=row["symbol"],
        earnings_date=row["earnings_date"],
        author=row["author"],
        body=row["body"],
        tags=tuple(_as_list(row["tags"])),
        created_at=row["created_at"],
    )


async def fetch_event_detail(
    conn,
    symbol: str,
    earnings_date: dt.date,
    *,
    event_filter: EventFilter | None = None,
) -> EventDetail | None:
    summary = await fetch_event_summary(
        conn, symbol, earnings_date, event_filter=event_filter
    )
    if summary is None:
        return None
    coverage = await fetch_coverage(conn, symbol, earnings_date)
    bars = await fetch_event_bars(conn, symbol, earnings_date, coverage)
    notes = await fetch_notes(conn, symbol, earnings_date)
    parameters = await _fetch_parameters(conn, summary, event_filter or EventFilter())
    return EventDetail(
        summary=summary,
        coverage=coverage,
        bars=bars,
        notes=notes,
        parameters=parameters,
    )


async def _fetch_parameters(
    conn, summary: EventSummary, event_filter: EventFilter
) -> dict[str, Any]:
    if not summary.analyzed:
        return {}
    value = await conn.fetchval(
        """
        SELECT parameters FROM earnings_reaction_features
        WHERE symbol = $1 AND earnings_date = $2
          AND feature_version = $3 AND detector_version = $4 AND classifier_version = $5
        """,
        summary.symbol,
        summary.earnings_date,
        event_filter.versions.feature_version,
        event_filter.versions.detector_version,
        event_filter.versions.classifier_version,
    )
    return _as_json_object(value)


# ------------------------------------------------------------------------- today


async def fetch_today(
    conn,
    market_date: dt.date,
    *,
    event_filter: EventFilter | None = None,
) -> tuple[TodayCandidate, ...]:
    """Today's after-close reporters with their provisional, clearly-labelled state."""
    base = dataclasses.replace(
        event_filter or EventFilter(),
        date_from=market_date,
        date_to=market_date,
        limit=None,
        offset=0,
        order_by="symbol_asc",
    )
    params = ParamBuilder()
    sql = f"SELECT * FROM ({_universe_cte(base, params)}) universe ORDER BY symbol"
    rows = await conn.fetch(sql, *params.params)
    if not rows:
        return ()

    symbols = [row["symbol"] for row in rows]
    quotes = await _latest_quotes(conn, symbols, market_date)
    pre_closes = await _pre_close_reference(conn, symbols, market_date)
    postmarket = await _provisional_postmarket_bars(conn, symbols, market_date)

    candidates: list[TodayCandidate] = []
    for row in rows:
        symbol = row["symbol"]
        quote = quotes.get(symbol)
        pre_close_ts, pre_close = pre_closes.get(symbol, (None, None))
        bars = postmarket.get(symbol, ())
        move, cross_ts, cross_pct = _provisional_path(pre_close, bars)
        candidates.append(
            TodayCandidate(
                symbol=symbol,
                earnings_date=row["earnings_date"],
                eps_estimate=row["eps_estimate"],
                eps_actual=row["eps_actual"],
                eps_surprise_pct=row["eps_surprise_pct"],
                pre_close_reference=pre_close,
                pre_close_source_ts=pre_close_ts,
                bid=quote["bid"] if quote else None,
                ask=quote["ask"] if quote else None,
                mark=quote["mark"] if quote else None,
                last=quote["last"] if quote else None,
                quote_volume=quote["volume"] if quote else None,
                quote_received_at=quote["gateway_received_at"] if quote else None,
                quote_stale=quote["stale"] if quote else None,
                quote_age_seconds=quote["age_seconds"] if quote else None,
                quote_quality_flags=(
                    tuple(_as_list(quote["data_quality_flags"])) if quote else ()
                ),
                provisional_move_pct=move,
                provisional_last_bar_ts=bars[-1][0] if bars else None,
                provisional_bar_minutes=len(bars),
                first_threshold_cross_ts=cross_ts,
                first_threshold_cross_pct=cross_pct,
                covered_phases=tuple(_as_list(row["covered_phases"])),
                finalized=row["analysis_status"] is not None,
                finalized_class=row["reaction_class"],
            )
        )
    return tuple(candidates)


async def _latest_quotes(
    conn, symbols: Sequence[str], earnings_date: dt.date
) -> dict[str, Mapping[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (symbol)
               symbol, bid, ask, mark, last, volume, gateway_received_at,
               stale, age_seconds, data_quality_flags
        FROM quote_evidence
        WHERE symbol = ANY($1::text[]) AND earnings_date = $2
        ORDER BY symbol, gateway_received_at DESC
        """,
        list(symbols),
        earnings_date,
    )
    return {row["symbol"]: row for row in rows}


async def _pre_close_reference(
    conn, symbols: Sequence[str], market_date: dt.date
) -> dict[str, tuple[dt.datetime | None, float | None]]:
    """The freshest observation of the final regular-session minute.

    Provisional by construction: it reads whatever bar evidence exists rather than
    the retrieval an audited coverage row names.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (symbol) symbol, ts, close
        FROM bar_evidence
        WHERE symbol = ANY($1::text[]) AND evidence_date = $2 AND session = 'regular'
        ORDER BY symbol, ts DESC, gateway_received_at DESC
        """,
        list(symbols),
        market_date,
    )
    return {row["symbol"]: (row["ts"], float(row["close"])) for row in rows}


async def _provisional_postmarket_bars(
    conn, symbols: Sequence[str], market_date: dt.date
) -> dict[str, tuple[tuple[dt.datetime, float], ...]]:
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (symbol, ts) symbol, ts, close
        FROM bar_evidence
        WHERE symbol = ANY($1::text[]) AND evidence_date = $2 AND session = 'extended'
          AND ts >= ($2::date + TIME '16:00') AT TIME ZONE 'America/New_York'
        ORDER BY symbol, ts, gateway_received_at DESC
        """,
        list(symbols),
        market_date,
    )
    by_symbol: dict[str, list[tuple[dt.datetime, float]]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append((row["ts"], float(row["close"])))
    return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}


def _provisional_path(
    pre_close: float | None,
    bars: Sequence[tuple[dt.datetime, float]],
) -> tuple[float | None, dt.datetime | None, float | None]:
    """Provisional move and first threshold cross from whatever minutes exist.

    Uses the same threshold as the finalized detector, and the same close-confirmed
    convention: the signal is knowable at the *end* of the crossing interval.
    """
    if not pre_close or pre_close <= 0 or not bars:
        return None, None, None
    move = (bars[-1][1] - pre_close) / pre_close * 100.0
    for ts, close in bars:
        change = (close - pre_close) / pre_close * 100.0
        if abs(change) >= REACTION_THRESHOLD_PCT:
            return move, ts + dt.timedelta(minutes=1), change
    return move, None, None


# ------------------------------------------------------------- quality and ops


async def fetch_operations_snapshot(conn) -> OperationsSnapshot:
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT max(created_at) FROM earnings_events) AS last_event,
            (SELECT max(retrieved_at) FROM earnings_ohlcv_coverage) AS last_coverage,
            (SELECT max(gateway_received_at) FROM bar_evidence) AS last_bar,
            (SELECT max(gateway_received_at) FROM quote_evidence) AS last_quote,
            (SELECT max(computed_at) FROM earnings_reaction_features) AS last_feature,
            (SELECT count(*)::int FROM earnings_events) AS events_total,
            (SELECT count(*)::int FROM earnings_events WHERE hour = 'amc') AS events_amc,
            (SELECT count(*)::int FROM earnings_ohlcv_coverage) AS coverage_rows,
            (SELECT count(*)::int FROM earnings_ohlcv_coverage
              WHERE collection_mode = 'scheduled_capture') AS scheduled_rows,
            (SELECT count(*)::int FROM earnings_ohlcv_coverage
              WHERE collection_mode = 'historical_backfill') AS backfill_rows,
            (SELECT count(*)::int FROM earnings_reaction_features) AS feature_rows
        """
    )
    return OperationsSnapshot(
        last_earnings_event_created_at=row["last_event"],
        last_coverage_retrieved_at=row["last_coverage"],
        last_bar_received_at=row["last_bar"],
        last_quote_received_at=row["last_quote"],
        last_feature_computed_at=row["last_feature"],
        events_total=row["events_total"],
        events_amc=row["events_amc"],
        coverage_rows=row["coverage_rows"],
        scheduled_coverage_rows=row["scheduled_rows"],
        backfill_coverage_rows=row["backfill_rows"],
        feature_rows=row["feature_rows"],
    )


async def fetch_quality_issues(
    conn,
    event_filter: EventFilter | None = None,
    *,
    limit: int = 500,
) -> tuple[QualityIssue, ...]:
    """Events that are not research-grade, and the specific reason for each.

    Kept out of the research leaderboard on purpose: a missing capture phase is an
    operations problem, and mixing it into reaction statistics invites reading a
    coverage gap as a market observation.
    """
    base = dataclasses.replace(
        event_filter or EventFilter(),
        analysis_statuses=(),
        reaction_classes=(),
        directions=(),
        limit=None,
        offset=0,
        order_by="earnings_date_desc",
    )
    params = ParamBuilder()
    sql = f"""
        SELECT * FROM ({_universe_cte(base, params)}) universe
        ORDER BY earnings_date DESC, symbol
        LIMIT {params.add(limit)}
    """
    rows = await conn.fetch(sql, *params.params)

    issues: list[QualityIssue] = []
    for row in rows:
        summary = EventSummary.from_row(row)
        missing_study_phases = tuple(
            phase for phase in STUDY_PHASES if phase not in summary.covered_phases
        )
        if missing_study_phases:
            issues.append(
                QualityIssue(
                    symbol=summary.symbol,
                    earnings_date=summary.earnings_date,
                    kind="missing_capture_phase",
                    detail=(
                        "study phase(s) never captured: " + ", ".join(missing_study_phases)
                    ),
                    phases_missing=missing_study_phases,
                    observed_minutes=None,
                    expected_minutes=None,
                )
            )
            continue
        if not summary.analyzed:
            issues.append(
                QualityIssue(
                    symbol=summary.symbol,
                    earnings_date=summary.earnings_date,
                    kind="not_analyzed",
                    detail=(
                        "evidence exists but no feature row for "
                        f"{base.versions.feature_version}/"
                        f"{base.versions.detector_version}/"
                        f"{base.versions.classifier_version}"
                    ),
                    phases_missing=(),
                    observed_minutes=None,
                    expected_minutes=None,
                )
            )
            continue
        if summary.analysis_status == "insufficient_data":
            issues.append(
                QualityIssue(
                    symbol=summary.symbol,
                    earnings_date=summary.earnings_date,
                    kind="insufficient_data",
                    detail=summary.analysis_status_reason or "insufficient data",
                    phases_missing=summary.missing_phases,
                    observed_minutes=summary.study_observed_minutes,
                    expected_minutes=STUDY_WINDOW_MINUTES,
                )
            )
            continue
        if summary.any_historical_backfill:
            issues.append(
                QualityIssue(
                    symbol=summary.symbol,
                    earnings_date=summary.earnings_date,
                    kind="historical_backfill",
                    detail=(
                        "recovered after the fact rather than captured on schedule "
                        f"(regular={summary.earnings_regular_collection_mode}, "
                        f"postmarket={summary.earnings_postmarket_collection_mode})"
                    ),
                    phases_missing=summary.missing_phases,
                    observed_minutes=summary.study_observed_minutes,
                    expected_minutes=STUDY_WINDOW_MINUTES,
                )
            )
        elif summary.data_quality_flags:
            issues.append(
                QualityIssue(
                    symbol=summary.symbol,
                    earnings_date=summary.earnings_date,
                    kind="data_quality_flag",
                    detail=", ".join(summary.data_quality_flags),
                    phases_missing=summary.missing_phases,
                    observed_minutes=summary.study_observed_minutes,
                    expected_minutes=STUDY_WINDOW_MINUTES,
                )
            )
    return tuple(issues)


async def fetch_class_distribution(
    conn, event_filter: EventFilter | None = None
) -> tuple[tuple[str, int], ...]:
    """Counts by reaction class over the filtered cohort, for the explorer charts."""
    base = dataclasses.replace(event_filter or EventFilter(), limit=None, offset=0)
    params = ParamBuilder()
    universe = _universe_cte(base, params)
    refinement = base.refinement_sql(params)
    rows = await conn.fetch(
        f"""
        WITH universe AS ({universe})
        SELECT COALESCE(reaction_class, 'not_analyzed') AS reaction_class,
               count(*)::int AS total
        FROM universe
        WHERE {refinement}
        GROUP BY 1
        ORDER BY 2 DESC, 1
        """,
        *params.params,
    )
    return tuple((row["reaction_class"], row["total"]) for row in rows)


async def fetch_monthly_counts(
    conn, event_filter: EventFilter | None = None
) -> tuple[tuple[str, int, int], ...]:
    """(month, analyzed, total) over the filtered cohort's universe."""
    base = dataclasses.replace(event_filter or EventFilter(), limit=None, offset=0)
    params = ParamBuilder()
    universe = _universe_cte(base, params)
    refinement = base.refinement_sql(params)
    rows = await conn.fetch(
        f"""
        WITH universe AS ({universe})
        SELECT to_char(date_trunc('month', earnings_date), 'YYYY-MM') AS month,
               count(*) FILTER (WHERE analysis_status = 'complete')::int AS analyzed,
               count(*)::int AS total
        FROM universe
        WHERE {refinement}
        GROUP BY 1
        ORDER BY 1
        """,
        *params.params,
    )
    return tuple((row["month"], row["analyzed"], row["total"]) for row in rows)

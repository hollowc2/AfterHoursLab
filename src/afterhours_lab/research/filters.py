"""Cohort filters for the shared research-data layer.

Every interface (CLI, notebooks, website) describes the cohort it wants with an
``EventFilter`` instead of writing its own SQL.  The filter validates itself on
construction, so an out-of-range threshold or an unknown reaction class fails at the
edge rather than silently selecting nothing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any

from afterhours_lab.canonical import canonical_digest, canonical_json
from afterhours_lab.reactions import CLASSIFIER_VERSION, DETECTOR_VERSION

# v2 (2026-09-25): the gateway's stale flag is no longer fatal for a phase whose
# response was received after the phase ended (see reactions._stale_is_fatal), so
# historical backfills are judged on their bars. v1 rows remain for audit.
FEATURE_VERSION = "earnings-reaction-v2"
ALGORITHM_NAME = "earnings_postmarket_reaction"

SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")

ANALYSIS_STATUSES = ("complete", "no_trigger_in_window", "insufficient_data")
REACTION_CLASSES = (
    "immediate_continuation",
    "spike_and_fade",
    "delayed_breakout",
    "whipsaw",
    "no_trigger_in_window",
)
REACTION_DIRECTIONS = ("up", "down", "flat")
COLLECTION_MODES = ("scheduled_capture", "historical_backfill")

MAX_LIMIT = 5000

# Whitelisted sort expressions.  A caller never supplies raw SQL; it names one of
# these keys and the layer substitutes the vetted fragment.
ORDER_BY = {
    "earnings_date_desc": "earnings_date DESC, symbol ASC",
    "earnings_date_asc": "earnings_date ASC, symbol ASC",
    "symbol_asc": "symbol ASC, earnings_date DESC",
    "initial_return_desc": "initial_return DESC NULLS LAST, earnings_date DESC",
    "initial_return_asc": "initial_return ASC NULLS LAST, earnings_date DESC",
    "abs_initial_return_desc": "abs(initial_return) DESC NULLS LAST, earnings_date DESC",
    "return_105m_desc": "return_105m DESC NULLS LAST, earnings_date DESC",
    "retention_desc": "retention DESC NULLS LAST, earnings_date DESC",
    "retention_asc": "retention ASC NULLS LAST, earnings_date DESC",
    "detection_delay_asc": "detection_delay_minutes ASC NULLS LAST, earnings_date DESC",
    "detection_delay_desc": "detection_delay_minutes DESC NULLS LAST, earnings_date DESC",
    "coverage_ratio_asc": "study_coverage_ratio ASC NULLS FIRST, earnings_date DESC",
    "volume_105m_desc": "volume_105m DESC NULLS LAST, earnings_date DESC",
    "eps_surprise_desc": "eps_surprise_pct DESC NULLS LAST, earnings_date DESC",
}


class ParamBuilder:
    """Accumulates asyncpg positional parameters and hands back ``$n`` placeholders."""

    def __init__(self) -> None:
        self._params: list[Any] = []

    def add(self, value: Any) -> str:
        self._params.append(value)
        return f"${len(self._params)}"

    @property
    def params(self) -> list[Any]:
        return list(self._params)


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"invalid equity symbol: {raw!r}")
    return symbol


@dataclasses.dataclass(frozen=True)
class FeatureVersions:
    """Which persisted feature generation a cohort reads.

    Features are keyed by the version triple, so mixing generations in one cohort
    would compare results produced by different definitions.  The default names the
    versions the currently installed code computes.
    """

    feature_version: str = FEATURE_VERSION
    detector_version: str = DETECTOR_VERSION
    classifier_version: str = CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field.name} must be a non-empty string")


@dataclasses.dataclass(frozen=True)
class EventFilter:
    """A described cohort of after-close earnings events.

    ``date_from``/``date_to`` and ``symbols`` define the *universe*; every other field
    refines it.  Queries report both counts so an interface can always disclose how
    many events were excluded rather than quietly showing a filtered subset.
    """

    date_from: dt.date | None = None
    date_to: dt.date | None = None
    symbols: tuple[str, ...] = ()
    analysis_statuses: tuple[str, ...] = ()
    reaction_classes: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    postmarket_collection_modes: tuple[str, ...] = ()
    min_abs_initial_return: float | None = None
    max_abs_initial_return: float | None = None
    min_retention: float | None = None
    max_retention: float | None = None
    min_detection_delay_minutes: int | None = None
    max_detection_delay_minutes: int | None = None
    min_volume_105m: int | None = None
    min_coverage_ratio: float | None = None
    min_eps_surprise_pct: float | None = None
    max_eps_surprise_pct: float | None = None
    versions: FeatureVersions = dataclasses.field(default_factory=FeatureVersions)
    order_by: str = "earnings_date_desc"
    limit: int | None = 200
    offset: int = 0

    def __post_init__(self) -> None:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        object.__setattr__(
            self, "symbols", tuple(dict.fromkeys(normalize_symbol(s) for s in self.symbols))
        )
        self._check_enum("analysis_statuses", ANALYSIS_STATUSES)
        self._check_enum("reaction_classes", REACTION_CLASSES)
        self._check_enum("directions", REACTION_DIRECTIONS)
        self._check_enum("postmarket_collection_modes", COLLECTION_MODES)
        if self.order_by not in ORDER_BY:
            raise ValueError(
                f"unknown order_by {self.order_by!r}; expected one of {sorted(ORDER_BY)}"
            )
        if self.limit is not None and not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if self.offset < 0:
            raise ValueError("offset must not be negative")
        if self.min_coverage_ratio is not None and not 0.0 <= self.min_coverage_ratio <= 1.0:
            raise ValueError("min_coverage_ratio must be between 0 and 1")
        self._check_range("min_abs_initial_return", "max_abs_initial_return")
        self._check_range("min_retention", "max_retention")
        self._check_range("min_detection_delay_minutes", "max_detection_delay_minutes")
        self._check_range("min_eps_surprise_pct", "max_eps_surprise_pct")

    def _check_enum(self, field: str, allowed: tuple[str, ...]) -> None:
        values = tuple(dict.fromkeys(getattr(self, field)))
        unknown = [value for value in values if value not in allowed]
        if unknown:
            raise ValueError(f"unknown {field}: {unknown}; expected any of {list(allowed)}")
        object.__setattr__(self, field, values)

    def _check_range(self, low_field: str, high_field: str) -> None:
        low, high = getattr(self, low_field), getattr(self, high_field)
        if low is not None and high is not None and low > high:
            raise ValueError(f"{low_field} must not exceed {high_field}")

    @property
    def has_refinements(self) -> bool:
        """Whether anything beyond the date/symbol universe narrows the cohort."""
        scope = {"date_from", "date_to", "symbols", "versions", "order_by", "limit", "offset"}
        defaults = EventFilter()
        return any(
            getattr(self, field.name) != getattr(defaults, field.name)
            for field in dataclasses.fields(self)
            if field.name not in scope
        )

    def scope_sql(self, params: ParamBuilder) -> str:
        """Predicate defining the universe: after-close events in range, by symbol.

        A liquidity-excluded event (see ``backfill_liquidity`` /
        ``archive_earnings.MIN_AVG_DOLLAR_VOLUME``) never enters the universe, the
        same as a thin name archive-earnings now keeps out going forward — it stays
        in ``earnings_events`` and every evidence table for audit, just outside every
        research view built on this filter.
        """
        clauses = ["e.hour = 'amc'", "e.liquidity_excluded_at IS NULL"]
        if self.date_from is not None:
            clauses.append(f"e.earnings_date >= {params.add(self.date_from)}")
        if self.date_to is not None:
            clauses.append(f"e.earnings_date <= {params.add(self.date_to)}")
        if self.symbols:
            clauses.append(f"e.symbol = ANY({params.add(list(self.symbols))}::text[])")
        return " AND ".join(clauses)

    def refinement_sql(self, params: ParamBuilder) -> str:
        """Predicate narrowing the universe. ``TRUE`` when nothing narrows it."""
        clauses: list[str] = []
        if self.analysis_statuses:
            clauses.append(
                f"analysis_status = ANY({params.add(list(self.analysis_statuses))}::text[])"
            )
        if self.reaction_classes:
            clauses.append(
                f"reaction_class = ANY({params.add(list(self.reaction_classes))}::text[])"
            )
        if self.directions:
            clauses.append(f"reaction_direction = ANY({params.add(list(self.directions))}::text[])")
        if self.postmarket_collection_modes:
            modes = list(self.postmarket_collection_modes)
            clauses.append(
                f"earnings_postmarket_collection_mode = ANY({params.add(modes)}::text[])"
            )
        if self.min_abs_initial_return is not None:
            clauses.append(f"abs(initial_return) >= {params.add(self.min_abs_initial_return)}")
        if self.max_abs_initial_return is not None:
            clauses.append(f"abs(initial_return) <= {params.add(self.max_abs_initial_return)}")
        if self.min_retention is not None:
            clauses.append(f"retention >= {params.add(self.min_retention)}")
        if self.max_retention is not None:
            clauses.append(f"retention <= {params.add(self.max_retention)}")
        if self.min_detection_delay_minutes is not None:
            value = self.min_detection_delay_minutes
            clauses.append(f"detection_delay_minutes >= {params.add(value)}")
        if self.max_detection_delay_minutes is not None:
            value = self.max_detection_delay_minutes
            clauses.append(f"detection_delay_minutes <= {params.add(value)}")
        if self.min_volume_105m is not None:
            clauses.append(f"volume_105m >= {params.add(self.min_volume_105m)}")
        if self.min_coverage_ratio is not None:
            clauses.append(f"study_coverage_ratio >= {params.add(self.min_coverage_ratio)}")
        if self.min_eps_surprise_pct is not None:
            clauses.append(f"eps_surprise_pct >= {params.add(self.min_eps_surprise_pct)}")
        if self.max_eps_surprise_pct is not None:
            clauses.append(f"eps_surprise_pct <= {params.add(self.max_eps_surprise_pct)}")
        return " AND ".join(clauses) if clauses else "TRUE"

    @property
    def order_sql(self) -> str:
        return ORDER_BY[self.order_by]


_PRESENTATION_FIELDS = {"order_by", "limit", "offset"}


def canonical_filter_record(event_filter: EventFilter) -> dict[str, Any]:
    """Stable study semantics, deliberately excluding presentation state."""
    record: dict[str, Any] = {}
    for field in dataclasses.fields(event_filter):
        if field.name in _PRESENTATION_FIELDS:
            continue
        value = getattr(event_filter, field.name)
        if field.name == "symbols":
            value = tuple(sorted(set(value)))
        elif isinstance(value, tuple):
            value = tuple(sorted(set(value)))
        record[field.name] = value
    return record


def canonical_filter_json(event_filter: EventFilter) -> str:
    return canonical_json(canonical_filter_record(event_filter))


def filter_digest(event_filter: EventFilter) -> str:
    return canonical_digest("event-filter-v1", canonical_filter_record(event_filter))


def refinement_exclusion_reasons(row: Any, event_filter: EventFilter) -> tuple[str, ...]:
    """Stable reason codes mirroring ``refinement_sql`` for frozen memberships."""
    reasons: list[str] = []
    checks = (
        (event_filter.analysis_statuses, row.analysis_status, "analysis_status"),
        (event_filter.reaction_classes, row.reaction_class, "reaction_class"),
        (event_filter.directions, row.reaction_direction, "reaction_direction"),
        (
            event_filter.postmarket_collection_modes,
            row.earnings_postmarket_collection_mode,
            "postmarket_collection_mode",
        ),
    )
    for allowed, value, code in checks:
        if allowed and value not in allowed:
            reasons.append(f"{code}_excluded")
    pairs = (
        (
            event_filter.min_abs_initial_return,
            abs(row.initial_return) if row.initial_return is not None else None,
            "min_abs_initial_return",
        ),
        (
            event_filter.max_abs_initial_return,
            abs(row.initial_return) if row.initial_return is not None else None,
            "max_abs_initial_return",
            "max",
        ),
        (event_filter.min_retention, row.retention, "min_retention"),
        (event_filter.max_retention, row.retention, "max_retention", "max"),
        (
            event_filter.min_detection_delay_minutes,
            row.detection_delay_minutes,
            "min_detection_delay",
        ),
        (
            event_filter.max_detection_delay_minutes,
            row.detection_delay_minutes,
            "max_detection_delay",
            "max",
        ),
        (event_filter.min_volume_105m, row.volume_105m, "min_volume_105m"),
        (event_filter.min_coverage_ratio, row.study_coverage_ratio, "min_coverage_ratio"),
        (event_filter.min_eps_surprise_pct, row.eps_surprise_pct, "min_eps_surprise_pct"),
        (event_filter.max_eps_surprise_pct, row.eps_surprise_pct, "max_eps_surprise_pct", "max"),
    )
    for item in pairs:
        threshold, value, code, *kind = item
        if threshold is None:
            continue
        if value is None or (value > threshold if kind else value < threshold):
            reasons.append(f"{code}_excluded")
    return tuple(reasons)

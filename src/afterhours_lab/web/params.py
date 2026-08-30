"""Translation between URL query strings and ``EventFilter``.

Kept apart from the route handlers because it is round-trippable and worth testing on
its own: a filter parsed from a query string, re-serialized, must describe the same
cohort.  That is what makes a shared explorer URL reproduce someone else's screen.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from urllib.parse import urlencode

from afterhours_lab.research import EventFilter, FeatureVersions

DEFAULT_LIMIT = 100
MAX_PAGE_LIMIT = 1000

# Query key -> filter field, for the single-valued numeric parameters.
NUMERIC_PARAMS = {
    "min_move": ("min_abs_initial_return", float),
    "max_move": ("max_abs_initial_return", float),
    "min_retention": ("min_retention", float),
    "max_retention": ("max_retention", float),
    "min_delay": ("min_detection_delay_minutes", int),
    "max_delay": ("max_detection_delay_minutes", int),
    "min_volume": ("min_volume_105m", int),
    "min_coverage": ("min_coverage_ratio", float),
    "min_eps_surprise": ("min_eps_surprise_pct", float),
    "max_eps_surprise": ("max_eps_surprise_pct", float),
}

LIST_PARAMS = {
    "symbol": "symbols",
    "status": "analysis_statuses",
    "class": "reaction_classes",
    "direction": "directions",
    "mode": "postmarket_collection_modes",
}


class QueryError(ValueError):
    """A malformed query string, reported to the caller rather than swallowed."""


def _get_list(params: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Accepts both repeated keys (?symbol=A&symbol=B) and comma-separated values."""
    getter = getattr(params, "getlist", None)
    raw = list(getter(key)) if getter else ([params[key]] if key in params else [])
    values: list[str] = []
    for item in raw:
        values.extend(part.strip() for part in str(item).split(",") if part.strip())
    return tuple(dict.fromkeys(values))


def _get_scalar(params: Mapping[str, object], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(params: Mapping[str, object], key: str) -> dt.date | None:
    raw = _get_scalar(params, key)
    if raw is None:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise QueryError(f"{key} must be an ISO date (YYYY-MM-DD), got {raw!r}") from exc


def parse_event_filter(
    params: Mapping[str, object], *, default_limit: int = DEFAULT_LIMIT
) -> EventFilter:
    fields: dict[str, object] = {
        "date_from": _parse_date(params, "from"),
        "date_to": _parse_date(params, "to"),
    }
    for key, field in LIST_PARAMS.items():
        values = _get_list(params, key)
        if values:
            fields[field] = values

    for key, (field, caster) in NUMERIC_PARAMS.items():
        raw = _get_scalar(params, key)
        if raw is None:
            continue
        try:
            fields[field] = caster(raw)
        except ValueError as exc:
            raise QueryError(f"{key} must be a number, got {raw!r}") from exc

    sort = _get_scalar(params, "sort")
    if sort:
        fields["order_by"] = sort

    limit = default_limit
    raw_limit = _get_scalar(params, "limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise QueryError(f"limit must be an integer, got {raw_limit!r}") from exc
        if not 1 <= limit <= MAX_PAGE_LIMIT:
            raise QueryError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    fields["limit"] = limit

    page = 1
    raw_page = _get_scalar(params, "page")
    if raw_page is not None:
        try:
            page = int(raw_page)
        except ValueError as exc:
            raise QueryError(f"page must be an integer, got {raw_page!r}") from exc
        if page < 1:
            raise QueryError("page must be 1 or greater")
    fields["offset"] = (page - 1) * limit

    versions = FeatureVersions(
        **{
            name: value
            for name, value in (
                ("feature_version", _get_scalar(params, "feature_version")),
                ("detector_version", _get_scalar(params, "detector_version")),
                ("classifier_version", _get_scalar(params, "classifier_version")),
            )
            if value is not None
        }
    )
    fields["versions"] = versions

    try:
        return EventFilter(**fields)  # type: ignore[arg-type]
    except ValueError as exc:
        raise QueryError(str(exc)) from exc


def page_number(event_filter: EventFilter) -> int:
    limit = event_filter.limit or DEFAULT_LIMIT
    return event_filter.offset // limit + 1


def filter_to_query(event_filter: EventFilter, **overrides: object) -> str:
    """Serialize a filter back to a query string, with optional overrides.

    Templates use this for sort headers and pagination so a link always carries the
    full cohort description rather than resetting the other filters.
    """
    pairs: list[tuple[str, str]] = []
    if event_filter.date_from:
        pairs.append(("from", event_filter.date_from.isoformat()))
    if event_filter.date_to:
        pairs.append(("to", event_filter.date_to.isoformat()))
    for key, field in LIST_PARAMS.items():
        for value in getattr(event_filter, field):
            pairs.append((key, value))
    for key, (field, _) in NUMERIC_PARAMS.items():
        value = getattr(event_filter, field)
        if value is not None:
            pairs.append((key, str(value)))
    pairs.append(("sort", event_filter.order_by))
    if event_filter.limit is not None:
        pairs.append(("limit", str(event_filter.limit)))
    page = page_number(event_filter)
    if page > 1:
        pairs.append(("page", str(page)))

    defaults = FeatureVersions()
    for name in ("feature_version", "detector_version", "classifier_version"):
        value = getattr(event_filter.versions, name)
        if value != getattr(defaults, name):
            pairs.append((name, value))

    for key, value in overrides.items():
        pairs = [pair for pair in pairs if pair[0] != key]
        if value is not None:
            pairs.append((key, str(value)))
    return urlencode(pairs)


def describe_filter(event_filter: EventFilter) -> list[tuple[str, str]]:
    """Human-readable (label, value) pairs of the active refinements, for display."""
    described: list[tuple[str, str]] = []
    scope = {"date_from", "date_to", "versions", "order_by", "limit", "offset"}
    defaults = EventFilter()
    for field in dataclasses.fields(event_filter):
        if field.name in scope:
            continue
        value = getattr(event_filter, field.name)
        if value == getattr(defaults, field.name):
            continue
        label = field.name.replace("_", " ")
        described.append(
            (label, ", ".join(value) if isinstance(value, tuple) else str(value))
        )
    return described

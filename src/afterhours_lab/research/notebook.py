"""Connection recipe and disclosure helpers for research notebooks.

Notebooks are *clients* of the research layer, not production code.  This module is
the one documented way they reach the database, so a notebook cell stays three
lines instead of fifteen:

    from afterhours_lab.research.notebook import research_pool
    from afterhours_lab.research import EventFilter, fetch_cohort

    pool = await research_pool()
    async with pool.acquire() as conn:
        cohort = await fetch_cohort(conn, EventFilter(...))

It adds nothing to the query path.  It is ``DatabasePool`` opened *read-only* (so a
notebook cannot write even by accident) plus two disclosure helpers that print a
cohort's included/excluded counts, its ``EventFilter``, and its version triple — the
things every notebook is required to state near the top.

Jupyter runs an event loop already, so ``await`` works at the top level of a cell.
Verify that against the kernel you actually installed (``jupyterlab`` from the
``notebooks`` extra) rather than assuming.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.research.datasets import CohortResult
from afterhours_lab.research.filters import EventFilter, FeatureVersions

__all__ = [
    "research_pool",
    "research_connection",
    "describe_filter",
    "describe_cohort",
    "show_cohort",
    "development_split",
]


async def research_pool(settings: DatabaseSettings | None = None) -> DatabasePool:
    """A read-only connection pool to the research database, from ``.env``.

    Open it once at the top of a notebook and reuse it; every acquired connection
    is fixed to ``TRANSACTION READ ONLY``.
    """
    return await DatabasePool.connect(settings or DatabaseSettings(), read_only=True)


@asynccontextmanager
async def research_connection(
    settings: DatabaseSettings | None = None,
) -> AsyncIterator[object]:
    """A one-shot read-only connection that opens and closes its own pool.

        async with research_connection() as conn:
            cohort = await fetch_cohort(conn, my_filter)

    Convenient for a short notebook; for many cells, prefer ``research_pool()`` so
    the pool is not rebuilt each time.
    """
    pool = await research_pool(settings)
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        await pool.close()


def _versions_line(versions: FeatureVersions) -> str:
    return (
        f"{versions.feature_version} / {versions.detector_version} "
        f"/ {versions.classifier_version}"
    )


def describe_filter(event_filter: EventFilter) -> str:
    """A plain-text description of a cohort's universe, refinements, and versions.

    Deliberately independent of ``web/params.describe_filter`` (which returns display
    pairs for a template): a notebook wants one printable block.
    """
    universe: list[str] = []
    universe.append(f"from {event_filter.date_from or 'open'}")
    universe.append(f"to {event_filter.date_to or 'open'}")
    if event_filter.symbols:
        universe.append(f"symbols={', '.join(event_filter.symbols)}")

    scope = {"date_from", "date_to", "symbols", "versions", "order_by", "limit", "offset"}
    defaults = EventFilter()
    refinements: list[str] = []
    for field in dataclasses.fields(event_filter):
        if field.name in scope:
            continue
        value = getattr(event_filter, field.name)
        if value == getattr(defaults, field.name):
            continue
        rendered = ", ".join(value) if isinstance(value, tuple) else str(value)
        refinements.append(f"{field.name.replace('_', ' ')}={rendered}")

    lines = [
        "EventFilter",
        f"  universe:     {'; '.join(universe)}",
        f"  refinements:  {'; '.join(refinements) if refinements else '(none)'}",
        f"  order_by:     {event_filter.order_by}",
        f"  limit/offset: {event_filter.limit}/{event_filter.offset}",
        f"  versions:     {_versions_line(event_filter.versions)}",
    ]
    return "\n".join(lines)


def describe_cohort(cohort: CohortResult) -> str:
    """The disclosure block every notebook must print near the top.

    Shows how many events the date/symbol universe held, how many survived the
    refinements, and — broken out — what was excluded, so a reader never mistakes a
    filtered subset for the whole population.
    """
    lines = [
        describe_filter(cohort.event_filter),
        "",
        f"universe:   {cohort.universe_count} events in date/symbol scope",
        f"included:   {cohort.included_count} after refinements",
        f"excluded:   {cohort.excluded_count}",
    ]
    if cohort.excluded_by_status:
        for status, total in cohort.excluded_by_status:
            lines.append(f"              - {status}: {total}")
    lines.append(
        f"page:       {cohort.page_count} rows loaded"
        + ("  (cohort is larger — raise limit/page)" if cohort.truncated else "")
    )
    return "\n".join(lines)


def show_cohort(cohort: CohortResult) -> CohortResult:
    """Print ``describe_cohort`` and return the cohort, so a cell can be::

        cohort = show_cohort(await fetch_cohort(conn, my_filter))
    """
    print(describe_cohort(cohort))
    return cohort


def development_split(
    date_from: dt.date, date_to: dt.date, *, holdout_fraction: float = 0.3
) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """Split a date range into a leading development window and a trailing
    out-of-sample window, by calendar days.

    The OOS window is meant to stay untouched until a study is otherwise complete;
    notebook 04 is the prototype of that discipline.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be between 0 and 1")
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    span = (date_to - date_from).days
    holdout_days = int(round(span * holdout_fraction))
    split = date_to - dt.timedelta(days=holdout_days)
    dev = (date_from, split - dt.timedelta(days=1))
    oos = (split, date_to)
    return dev, oos

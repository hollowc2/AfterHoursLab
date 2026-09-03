"""Read-only research website: FastAPI, server-rendered HTML, HTMX for partials.

The website displays and orchestrates research; it does not reimplement research
calculations.  Every number on every page comes from ``afterhours_lab.research``,
which in turn reads persisted features computed by ``afterhours_lab.reactions``.

The one write path is the researcher note, which is discretionary commentary stored in
its own table and never mixed into computed features.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from afterhours_lab.config import AppSettings
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.gateway import BoundedGatewayClient, build_gateway_client
from afterhours_lab.reactions import REACTION_THRESHOLD_PCT
from afterhours_lab.research import (
    ANALYSIS_STATUSES,
    COLLECTION_MODES,
    ORDER_BY,
    REACTION_CLASSES,
    REACTION_DIRECTIONS,
    STUDY_WINDOW_MINUTES,
    EventFilter,
    add_note,
    fetch_class_distribution,
    fetch_cohort,
    fetch_event_detail,
    fetch_monitor_health,
    fetch_monthly_counts,
    fetch_operations_snapshot,
    fetch_quality_issues,
    fetch_today,
    normalize_symbol,
    to_csv,
)
from afterhours_lab.web import charts
from afterhours_lab.web.params import (
    QueryError,
    describe_filter,
    filter_to_query,
    page_number,
    parse_event_filter,
)

EASTERN = ZoneInfo("America/New_York")
PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

DEFAULT_EXPLORER_DAYS = 180
DAILY_HISTORY_DAYS = 365
CSV_MAX_ROWS = 5000
CHART_MAX_ROWS = 2000
SSE_POLL_SECONDS = 2.0
SSE_KEEPALIVE_SECONDS = 15.0


async def _today_event_stream(
    request: Request,
    market_date: dt.date,
    *,
    historical: bool,
    load_snapshot: Callable[[Request, dt.date], Awaitable[dict[str, Any]]],
    snapshot_digest: Callable[[dict[str, Any]], str],
    snapshot_event: Callable[[dict[str, Any], str], str],
    poll_seconds: float = SSE_POLL_SECONDS,
    keepalive_seconds: float = SSE_KEEPALIVE_SECONDS,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] | None = None,
) -> AsyncIterator[str]:
    """Yield deduplicated SSE frames without holding resources while sleeping."""
    now = clock or (lambda: asyncio.get_running_loop().time())
    previous_digest: str | None = None
    last_frame_at = now()
    while not await request.is_disconnected():
        try:
            snapshot = await load_snapshot(request, market_date)
            digest = snapshot_digest(snapshot)
            if digest != previous_digest:
                yield snapshot_event(snapshot, digest)
                previous_digest = digest
                last_frame_at = now()
            if historical:
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            payload = json.dumps(
                {
                    "state": "unavailable",
                    "message": (
                        "Database state is temporarily unavailable; "
                        "no market data was substituted."
                    ),
                },
                separators=(",", ":"),
            )
            yield f"event: degraded\ndata: {payload}\n\n"
            last_frame_at = now()
            if historical:
                return

        await sleeper(poll_seconds)
        if now() - last_frame_at >= keepalive_seconds:
            yield ": keepalive\n\n"
            last_frame_at = now()


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%" if value is not None else "—"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return f"{value:,.{digits}f}" if value is not None else "—"


def _fmt_int(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.2f}x" if value is not None else "—"


def _fmt_et(value: dt.datetime | None, with_date: bool = False) -> str:
    if value is None:
        return "—"
    eastern = value.astimezone(EASTERN)
    return eastern.strftime("%Y-%m-%d %H:%M:%S ET" if with_date else "%H:%M:%S ET")


def _fmt_age(value: dt.datetime | None) -> str:
    if value is None:
        return "never"
    seconds = (dt.datetime.now(dt.timezone.utc) - value).total_seconds()
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def _short_hash(value: str | None) -> str:
    return f"{value[:12]}…" if value else "—"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        pct=_fmt_pct,
        num=_fmt_num,
        int_=_fmt_int,
        ratio=_fmt_ratio,
        et=_fmt_et,
        age=_fmt_age,
        short_hash=_short_hash,
    )
    templates.env.globals.update(
        query=filter_to_query,
        describe_filter=describe_filter,
        page_number=page_number,
        ORDER_BY=ORDER_BY,
        ANALYSIS_STATUSES=ANALYSIS_STATUSES,
        REACTION_CLASSES=REACTION_CLASSES,
        REACTION_DIRECTIONS=REACTION_DIRECTIONS,
        COLLECTION_MODES=COLLECTION_MODES,
        THRESHOLD_PCT=REACTION_THRESHOLD_PCT,
        STUDY_WINDOW_MINUTES=STUDY_WINDOW_MINUTES,
    )
    return templates


def create_app(
    pool: DatabasePool | None = None,
    gateway: BoundedGatewayClient | None = None,
) -> FastAPI:
    """Build the app. Passing ``pool`` skips external startup, which tests use."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if pool is not None:
            app.state.pool = pool
            app.state.gateway = gateway
            yield
            return
        connected = await DatabasePool.connect(DatabaseSettings())
        connected_gateway = build_gateway_client(AppSettings())
        app.state.pool = connected
        app.state.gateway = connected_gateway
        try:
            yield
        finally:
            await connected_gateway.close()
            await connected.close()

    app = FastAPI(
        title="AfterHoursLab research",
        description="Read-only interface over the shared research-data layer.",
        lifespan=lifespan,
    )
    app.state.pool = pool
    app.state.gateway = gateway
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = build_templates()
    app.state.templates = templates

    def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    @app.exception_handler(QueryError)
    async def _query_error(request: Request, exc: QueryError) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=400
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/today")

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz(request: Request) -> str:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return "ok"

    # ------------------------------------------------------------------- today

    def parse_market_date(date: str | None) -> dt.date:
        try:
            return dt.date.fromisoformat(date) if date else dt.datetime.now(EASTERN).date()
        except ValueError as exc:
            raise QueryError(f"date must be an ISO date, got {date!r}") from exc

    async def load_today(request: Request, market_date: dt.date) -> dict[str, Any]:
        async with request.app.state.pool.acquire() as conn:
            candidates = await fetch_today(conn, market_date)
            operations = await fetch_operations_snapshot(conn)
            monitor_health = await fetch_monitor_health(conn, market_date)
        return {
            "market_date": market_date,
            "candidates": candidates,
            "operations": operations,
            "monitor_health": monitor_health,
            "generated_at": dt.datetime.now(dt.timezone.utc),
        }

    @app.get("/today", response_class=HTMLResponse)
    async def today(request: Request, date: str | None = None) -> HTMLResponse:
        market_date = parse_market_date(date)
        snapshot = await load_today(request, market_date)
        is_live_date = market_date == dt.datetime.now(EASTERN).date()
        return render(
            request,
            "today.html",
            {
                "page": "today",
                "previous_date": market_date - dt.timedelta(days=1),
                "next_date": market_date + dt.timedelta(days=1),
                "is_live_date": is_live_date,
                **snapshot,
            },
        )

    def snapshot_digest(snapshot: dict[str, Any]) -> str:
        semantic = {
            "candidates": [row.to_record() for row in snapshot["candidates"]],
            "operations": snapshot["operations"].to_record(),
            "monitor_health": snapshot["monitor_health"].to_record(),
        }
        encoded = json.dumps(semantic, default=str, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def snapshot_event(snapshot: dict[str, Any], event_id: str) -> str:
        html = templates.get_template("_today_snapshot.html").render(snapshot)
        payload = json.dumps(
            {
                "html": html,
                "market_date": snapshot["market_date"].isoformat(),
                "generated_at": snapshot["generated_at"].isoformat(),
                "state": "connected",
            },
            separators=(",", ":"),
        )
        return f"id: {event_id}\nevent: snapshot\ndata: {payload}\n\n"

    @app.get("/today/stream")
    async def today_stream(request: Request, date: str | None = None) -> StreamingResponse:
        market_date = parse_market_date(date)
        historical = market_date != dt.datetime.now(EASTERN).date()

        return StreamingResponse(
            _today_event_stream(
                request,
                market_date,
                historical=historical,
                load_snapshot=load_today,
                snapshot_digest=snapshot_digest,
                snapshot_event=snapshot_event,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---------------------------------------------------------------- explorer

    def explorer_filter(request: Request) -> EventFilter:
        return parse_event_filter(_with_default_dates(request.query_params))

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request) -> HTMLResponse:
        event_filter = explorer_filter(request)
        # Charts describe the whole cohort, not the visible page, so they are built
        # from an unpaged read capped at CHART_MAX_ROWS.
        chart_filter = dataclasses.replace(event_filter, limit=CHART_MAX_ROWS, offset=0)
        async with request.app.state.pool.acquire() as conn:
            cohort = await fetch_cohort(conn, event_filter)
            chart_cohort = await fetch_cohort(conn, chart_filter)
            distribution = await fetch_class_distribution(conn, event_filter)
            months = await fetch_monthly_counts(conn, event_filter)
        return render(
            request,
            "explorer.html",
            {
                "page": "explorer",
                "filter": event_filter,
                "cohort": cohort,
                "chart_cohort": chart_cohort,
                "figures": {
                    "initial_vs_final": charts.initial_vs_final_figure(chart_cohort.rows),
                    "delay_vs_retention": charts.delay_vs_retention_figure(chart_cohort.rows),
                    "surprise_vs_reaction": charts.surprise_vs_reaction_figure(
                        chart_cohort.rows
                    ),
                    "class_distribution": charts.class_distribution_figure(distribution),
                    "monthly": charts.monthly_coverage_figure(months),
                },
            },
        )

    @app.get("/events/rows", response_class=HTMLResponse)
    async def event_rows(request: Request) -> HTMLResponse:
        """HTMX partial: the results table alone, for filter and pagination updates."""
        event_filter = explorer_filter(request)
        async with request.app.state.pool.acquire() as conn:
            cohort = await fetch_cohort(conn, event_filter)
        return render(
            request, "_event_table.html", {"filter": event_filter, "cohort": cohort}
        )

    @app.get("/events.csv")
    async def events_csv(request: Request) -> PlainTextResponse:
        event_filter = explorer_filter(request)
        export_filter = dataclasses.replace(event_filter, limit=CSV_MAX_ROWS, offset=0)
        async with request.app.state.pool.acquire() as conn:
            cohort = await fetch_cohort(conn, export_filter)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return PlainTextResponse(
            to_csv(cohort.rows),
            media_type="text/csv",
            headers={
                "content-disposition": (
                    f'attachment; filename="afterhourslab-events-{stamp}.csv"'
                )
            },
        )

    # ------------------------------------------------------------ event detail

    async def load_detail(request: Request, symbol: str, earnings_date: str):
        try:
            clean_symbol = normalize_symbol(symbol)
            date = dt.date.fromisoformat(earnings_date)
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        async with request.app.state.pool.acquire() as conn:
            detail = await fetch_event_detail(conn, clean_symbol, date)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail=f"no earnings event for {clean_symbol} on {date.isoformat()}",
            )
        return detail

    @app.get("/events/{symbol}/{earnings_date}", response_class=HTMLResponse)
    async def event_detail(
        request: Request, symbol: str, earnings_date: str
    ) -> HTMLResponse:
        detail = await load_detail(request, symbol, earnings_date)
        daily_figure = None
        daily_context_message = None
        if request.app.state.gateway is None:
            daily_context_message = "Daily history is unavailable in this environment."
        else:
            try:
                daily_history = await request.app.state.gateway.get_history(
                    detail.summary.symbol,
                    frequency="daily",
                    days_back=DAILY_HISTORY_DAYS,
                )
                history = daily_history.history
                if history.symbol != detail.summary.symbol or history.frequency != "daily":
                    daily_context_message = "Daily history returned an unexpected identity."
                else:
                    daily_figure = charts.daily_context_figure(
                        detail.summary.symbol,
                        detail.summary.earnings_date,
                        history.bars,
                        reference_price=detail.summary.reference_price,
                    )
                    if daily_figure is None:
                        daily_context_message = "No daily bars were returned for this symbol."
            except asyncio.CancelledError:
                raise
            except Exception:
                daily_context_message = "Daily history is temporarily unavailable."
        return render(
            request,
            "event.html",
            {
                "page": "explorer",
                "detail": detail,
                "summary": detail.summary,
                "figure": charts.event_figure(detail),
                "daily_figure": daily_figure,
                "daily_context_message": daily_context_message,
                "following_figure": charts.following_day_figure(detail),
                "notebook_snippet": _notebook_snippet(
                    detail.summary.symbol, detail.summary.earnings_date
                ),
            },
        )

    @app.get("/events/{symbol}/{earnings_date}/chart.json")
    async def event_chart(
        request: Request, symbol: str, earnings_date: str
    ) -> JSONResponse:
        detail = await load_detail(request, symbol, earnings_date)
        return JSONResponse(
            {
                "earnings_day": charts.event_figure(detail),
                "following_day": charts.following_day_figure(detail),
            }
        )

    @app.post("/events/{symbol}/{earnings_date}/notes")
    async def create_note(
        request: Request,
        symbol: str,
        earnings_date: str,
        author: str = Form(...),
        body: str = Form(...),
        tags: str = Form(""),
    ) -> RedirectResponse:
        try:
            clean_symbol = normalize_symbol(symbol)
            date = dt.date.fromisoformat(earnings_date)
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        try:
            async with request.app.state.pool.acquire() as conn:
                await add_note(
                    conn,
                    clean_symbol,
                    date,
                    author=author,
                    body=body,
                    tags=tags.split(","),
                )
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        return RedirectResponse(
            f"/events/{clean_symbol}/{date.isoformat()}#notes", status_code=303
        )

    # ---------------------------------------------------------------- quality

    @app.get("/quality", response_class=HTMLResponse)
    async def quality(request: Request) -> HTMLResponse:
        event_filter = explorer_filter(request)
        async with request.app.state.pool.acquire() as conn:
            operations = await fetch_operations_snapshot(conn)
            monitor_health = await fetch_monitor_health(conn, dt.datetime.now(EASTERN).date())
            issues = await fetch_quality_issues(conn, event_filter)
        grouped: dict[str, list] = {}
        for issue in issues:
            grouped.setdefault(issue.kind, []).append(issue)
        return render(
            request,
            "quality.html",
            {
                "page": "quality",
                "filter": event_filter,
                "operations": operations,
                "monitor_health": monitor_health,
                "issues": issues,
                "grouped": grouped,
            },
        )

    return app


def _with_default_dates(params: QueryParams) -> QueryParams:
    """Explorer default: a trailing window, so a bare ``/events`` is not a full scan.

    Only supplied when the caller named neither bound; a caller who sets just one gets
    the open-ended range it asked for.
    """
    if params.get("from") or params.get("to"):
        return params
    today_et = dt.datetime.now(EASTERN).date()
    pairs = list(params.multi_items())
    pairs.append(("from", (today_et - dt.timedelta(days=DEFAULT_EXPLORER_DAYS)).isoformat()))
    pairs.append(("to", today_et.isoformat()))
    return QueryParams(pairs)


def _notebook_snippet(symbol: str, earnings_date: dt.date) -> str:
    return (
        "import datetime as dt\n"
        "from afterhours_lab.db.config import DatabaseSettings\n"
        "from afterhours_lab.db.connection import DatabasePool\n"
        "from afterhours_lab.research import fetch_event_detail\n\n"
        "pool = await DatabasePool.connect(DatabaseSettings())\n"
        "async with pool.acquire() as conn:\n"
        f"    detail = await fetch_event_detail(conn, {symbol!r}, "
        f"dt.date({earnings_date.year}, {earnings_date.month}, {earnings_date.day}))\n"
        "detail.summary"
    )


app = create_app()


def main() -> None:
    """Run the development server, bound to loopback only."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the AfterHoursLab research website")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8055)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "afterhours_lab.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()

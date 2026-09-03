from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

import pytest
from conftest import (
    EARNINGS_DATE,
    POSTMARKET_START,
    FakeConnection,
    FakePool,
    bar_row,
    coverage_row,
    event_row,
    note_row,
)
from fastapi.testclient import TestClient
from schwab_gateway_sdk.models import HistoryResponseV1

from afterhours_lab import gateway as gateway_module
from afterhours_lab.web.app import _today_event_stream, create_app

DATE = EARNINGS_DATE.isoformat()


class FakeGateway:
    def __init__(self, response: HistoryResponseV1) -> None:
        self.response = response
        self.requests: list[tuple[str, str, int | None]] = []

    async def get_history(
        self, symbol: str, *, frequency: str = "minute", days_back: int | None = None
    ) -> HistoryResponseV1:
        self.requests.append((symbol, frequency, days_back))
        return self.response


def client_for(conn: FakeConnection, gateway=None) -> TestClient:
    return TestClient(
        create_app(FakePool(conn), gateway=gateway),
        backend_options={"use_uvloop": True},
    )


def daily_history_response() -> HistoryResponseV1:
    return HistoryResponseV1.model_validate(
        {
            "schema_version": "1.0",
            "history": {
                "symbol": "TEST",
                "frequency": "daily",
                "bars": [
                    {
                        "timestamp": dt.datetime(2026, 8, day, 4, tzinfo=dt.timezone.utc),
                        "open": 95.0 + day,
                        "high": 96.0 + day,
                        "low": 94.0 + day,
                        "close": 95.5 + day,
                        "volume": 1_000_000 + day,
                    }
                    for day in range(18, 23)
                ],
                "gateway_received_at": POSTMARKET_START,
                "source": "schwab",
                "stale": False,
                "data_quality_flags": [],
            },
        }
    )


@pytest.fixture
def full_conn() -> FakeConnection:
    return FakeConnection(
        events=[event_row()],
        universe_count=5,
        included_count=1,
        excluded_by_status={"insufficient_data": 4},
        coverage=[coverage_row("earnings_regular"), coverage_row("earnings_postmarket")],
        phase_bars=[bar_row(0), bar_row(1, 103.0), bar_row(4, 104.0)],
        notes=[note_row()],
        quotes=[
            {
                "symbol": "TEST",
                "bid": 103.0,
                "ask": 103.4,
                "mark": 103.2,
                "last": 103.1,
                "volume": 5_000,
                "gateway_received_at": POSTMARKET_START,
                "stale": False,
                "age_seconds": 2.0,
                "data_quality_flags": [],
            }
        ],
        pre_close=[{"symbol": "TEST", "ts": POSTMARKET_START, "close": 100.0}],
        postmarket=[{"symbol": "TEST", "ts": POSTMARKET_START, "close": 103.0}],
        distribution=[{"reaction_class": "immediate_continuation", "total": 1}],
        monthly=[{"month": "2026-08", "analyzed": 1, "total": 5}],
    )


def test_root_redirects_to_today(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/today"


def test_healthz_reports_a_live_connection(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        assert client.get("/healthz").text == "ok"


def test_today_labels_provisional_and_finalized_state(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        body = client.get(f"/today?date={DATE}").text

    assert "provisional" in body
    assert "finalized" in body
    assert "TEST" in body
    # The provisional move is computed server-side from the pre-close reference.
    assert "+3.00%" in body


def test_today_rejects_a_malformed_date(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.get("/today?date=yesterday")
    assert response.status_code == 400
    assert "must be an ISO date" in response.text


def test_historical_today_page_uses_one_shot_sse_and_disables_reconnect(
    full_conn: FakeConnection,
) -> None:
    with client_for(full_conn) as client:
        page = client.get(f"/today?date={DATE}")
        stream = client.get(f"/today/stream?date={DATE}")

    assert "Historical snapshot — live updates disabled" in page.text
    assert "/static/today.js" not in page.text
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache, no-transform"
    assert stream.headers["x-accel-buffering"] == "no"
    assert "event: snapshot" in stream.text
    assert "id: " in stream.text
    assert "TEST" in stream.text


def test_today_stream_rejects_a_malformed_date(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.get("/today/stream?date=nope")
    assert response.status_code == 400


class DisconnectSequence:
    def __init__(self, *states: bool) -> None:
        self.states = iter(states)

    async def is_disconnected(self) -> bool:
        return next(self.states, True)


async def collect_stream(request, loader, **overrides):
    return [
        frame
        async for frame in _today_event_stream(
            request,
            EARNINGS_DATE,
            historical=False,
            load_snapshot=loader,
            snapshot_digest=lambda snapshot: str(snapshot["version"]),
            snapshot_event=lambda snapshot, digest: (
                f"id: {digest}\nevent: snapshot\ndata: {snapshot['version']}\n\n"
            ),
            **overrides,
        )
    ]


async def test_today_stream_deduplicates_unchanged_snapshots() -> None:
    versions = iter((1, 1, 2))

    async def loader(_request, _date):
        return {"version": next(versions)}

    async def sleeper(_delay):
        return None

    frames = await collect_stream(
        DisconnectSequence(False, False, False, True),
        loader,
        sleeper=sleeper,
        keepalive_seconds=999,
    )

    assert [frame.splitlines()[0] for frame in frames] == ["id: 1", "id: 2"]


async def test_today_stream_emits_keepalive_without_duplicate_data() -> None:
    elapsed = 0.0

    async def loader(_request, _date):
        return {"version": 1}

    async def sleeper(delay):
        nonlocal elapsed
        elapsed += delay

    frames = await collect_stream(
        DisconnectSequence(False, False, False, True),
        loader,
        poll_seconds=5,
        keepalive_seconds=10,
        sleeper=sleeper,
        clock=lambda: elapsed,
    )

    assert sum("event: snapshot" in frame for frame in frames) == 1
    assert frames.count(": keepalive\n\n") == 1


async def test_today_stream_discloses_database_failure_without_empty_market_data() -> None:
    async def loader(_request, _date):
        raise RuntimeError("database unavailable")

    frames = [
        frame
        async for frame in _today_event_stream(
            DisconnectSequence(False),
            EARNINGS_DATE,
            historical=True,
            load_snapshot=loader,
            snapshot_digest=lambda _snapshot: "unused",
            snapshot_event=lambda _snapshot, _digest: "unused",
        )
    ]

    assert len(frames) == 1
    assert "event: degraded" in frames[0]
    assert "no market data was substituted" in frames[0]
    assert "database unavailable" not in frames[0]


async def test_today_stream_honors_disconnect_before_acquiring_data() -> None:
    calls = []

    async def loader(_request, _date):
        calls.append("load")
        return {"version": 1}

    frames = await collect_stream(DisconnectSequence(True), loader)

    assert frames == []
    assert calls == []


async def test_today_stream_generator_closes_without_leaking_more_work() -> None:
    calls = []

    async def loader(_request, _date):
        calls.append("load")
        return {"version": len(calls)}

    async def sleeper(_delay):
        calls.append("sleep")

    stream = _today_event_stream(
        DisconnectSequence(False, False),
        EARNINGS_DATE,
        historical=False,
        load_snapshot=loader,
        snapshot_digest=lambda snapshot: str(snapshot["version"]),
        snapshot_event=lambda _snapshot, digest: f"id: {digest}\n\n",
        sleeper=sleeper,
    )
    assert await anext(stream) == "id: 1\n\n"
    await stream.aclose()
    await asyncio.sleep(0)

    assert calls == ["load"]


def test_web_routes_never_construct_a_gateway_client(
    full_conn: FakeConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("web route constructed a gateway client")

    monkeypatch.setattr(gateway_module, "build_gateway_client", forbidden)
    with client_for(full_conn) as client:
        assert client.get(f"/today?date={DATE}").status_code == 200
        assert client.get(f"/today/stream?date={DATE}").status_code == 200


def test_explorer_discloses_included_and_excluded_counts(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        body = client.get("/events").text

    assert "of the\n    5 after-close events" in body.replace("\r", "") or "5 after-close" in body
    assert "insufficient_data 4" in body
    assert "immediate_continuation" in body


def test_explorer_rejects_an_unknown_filter_value(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.get("/events?class=moon_shot")
    assert response.status_code == 400
    assert "unknown reaction_classes" in response.text


def test_explorer_rows_partial_renders_without_the_page_chrome(
    full_conn: FakeConnection,
) -> None:
    with client_for(full_conn) as client:
        body = client.get("/events/rows").text

    assert "<table>" in body
    assert "<html" not in body


def test_csv_export_is_attached_and_has_the_dataset_header(
    full_conn: FakeConnection,
) -> None:
    with client_for(full_conn) as client:
        response = client.get("/events.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert response.text.splitlines()[0].startswith("symbol,earnings_date")


def test_event_page_renders_the_chart_and_evidence(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        body = client.get(f"/events/TEST/{DATE}").text

    assert "data-figure=" in body
    assert "Phase coverage" in body
    assert "Researcher notes" in body
    assert "fetch_event_detail" in body  # the notebook snippet
    assert "aaaaaaaaaaaa" in body  # the response hash, truncated for display


def test_event_page_adds_daily_context_from_gateway(full_conn: FakeConnection) -> None:
    gateway = FakeGateway(daily_history_response())
    with client_for(full_conn, gateway) as client:
        body = client.get(f"/events/TEST/{DATE}").text

    assert "Daily context" in body
    assert "daily context around earnings" in body
    figures = [json.loads(value) for value in re.findall(r"data-figure='([^']+)'", body)]
    daily = next(
        figure
        for figure in figures
        if "daily context around earnings" in figure["layout"]["title"]["text"]
    )
    assert daily["data"][0]["type"] == "candlestick"
    assert any(shape["label"]["text"] == "earnings" for shape in daily["layout"]["shapes"])
    assert gateway.requests == [("TEST", "daily", 365)]


def test_event_page_is_404_for_an_unknown_event() -> None:
    with client_for(FakeConnection(events=[])) as client:
        response = client.get(f"/events/NOPE/{DATE}")
    assert response.status_code == 404


def test_event_page_rejects_a_malformed_symbol(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.get(f"/events/not%20a%20symbol/{DATE}")
    assert response.status_code == 400


def test_chart_json_contains_server_computed_traces(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        payload = client.get(f"/events/TEST/{DATE}/chart.json").json()

    earnings_day = payload["earnings_day"]
    assert {trace["type"] for trace in earnings_day["data"]} <= {
        "candlestick",
        "bar",
        "scatter",
    }
    # The 2% detection bands and the pre-close reference are drawn as shapes.
    assert len(earnings_day["layout"]["shapes"]) >= 4


def test_chart_shapes_are_anchored_to_the_stored_reference_price(
    full_conn: FakeConnection,
) -> None:
    with client_for(full_conn) as client:
        payload = client.get(f"/events/TEST/{DATE}/chart.json").json()

    lines = [
        shape
        for shape in payload["earnings_day"]["layout"]["shapes"]
        if shape["type"] == "line"
    ]
    levels = {round(shape["y0"], 4) for shape in lines}
    assert 100.0 in levels  # reference
    assert 102.0 in levels  # +2%
    assert 98.0 in levels  # -2%


def test_note_submission_redirects_back_to_the_event(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.post(
            f"/events/TEST/{DATE}/notes",
            data={"author": "researcher", "body": "faded", "tags": "fade"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/TEST/{DATE}#notes"


def test_blank_note_is_rejected(full_conn: FakeConnection) -> None:
    with client_for(full_conn) as client:
        response = client.post(
            f"/events/TEST/{DATE}/notes",
            data={"author": "researcher", "body": "   ", "tags": ""},
        )
    assert response.status_code == 400
    assert "note body must not be blank" in response.text


def test_quality_page_separates_operations_from_research(full_conn: FakeConnection) -> None:
    conn = FakeConnection(
        events=[event_row(symbol="MISS", covered_phases=["following_regular"])]
    )
    with client_for(conn) as client:
        body = client.get("/quality").text

    assert "Last successful writes" in body
    assert "missing_capture_phase" in body
    assert "historical backfill" in body


def test_figures_are_embedded_as_parseable_escaped_json(full_conn: FakeConnection) -> None:
    """Jinja's tojson escapes ', <, > and & but not ", so the attribute must be
    single-quoted or the JSON would terminate it early and land in markup context."""
    with client_for(full_conn) as client:
        body = client.get("/events").text

    assert 'data-figure="' not in body
    start = body.index("data-figure='") + len("data-figure='")
    end = body.index("'", start)
    figure = json.loads(body[start:end])
    assert "data" in figure and "layout" in figure
    assert "<" not in body[start:end]

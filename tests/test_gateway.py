import asyncio
import datetime as dt

import httpx
import pytest
from schwab_gateway_sdk.client import (
    GatewayAuthenticationError,
    GatewayAuthorizationError,
    GatewayCapacityError,
    GatewayResponseError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)

from afterhours_lab.config import AppSettings
from afterhours_lab.gateway import build_gateway_client

SPOT_PAYLOAD = {
    "schema_version": "1.0",
    "spot": {
        "symbol": "$SPX",
        "price": 6300.5,
        "event_timestamp": None,
        "gateway_received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "test",
        "stale": False,
        "age_seconds": None,
        "data_quality_flags": [],
    },
}

HISTORY_PAYLOAD = {
    "schema_version": "1.0",
    "history": {
        "symbol": "AAPL",
        "frequency": "minute",
        "bars": [
            {
                "timestamp": "2026-08-25T20:00:00Z",
                "open": 225.0,
                "high": 226.0,
                "low": 224.5,
                "close": 225.5,
                "volume": 1200,
            }
        ],
        "event_timestamp": "2026-08-25T20:00:00Z",
        "gateway_received_at": "2026-08-25T20:00:01Z",
        "source": "schwab",
        "stale": False,
        "age_seconds": 1.0,
        "data_quality_flags": [],
    },
}

SESSION_HISTORY_PAYLOAD = {
    "schema_version": "1.0",
    "session_history": {
        "symbol": "AAPL",
        "date": "2026-08-25",
        "session": "extended",
        "candles": HISTORY_PAYLOAD["history"]["bars"],
        "event_timestamp": "2026-08-25T20:00:00Z",
        "gateway_received_at": "2026-08-25T20:00:01Z",
        "source": "schwab",
        "stale": False,
        "age_seconds": 1.0,
        "data_quality_flags": [],
    },
}


def make_settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "SCHWAB_GATEWAY_URL": "https://gateway.internal",
            "SCHWAB_GATEWAY_API_KEY": "test-key",
        }
    )


async def test_build_gateway_client_fetches_spot_through_mocked_transport() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, json=SPOT_PAYLOAD)

    transport = httpx.MockTransport(handler)
    fake_client = httpx.AsyncClient(base_url="https://gateway.internal", transport=transport)

    gateway = build_gateway_client(make_settings(), client=fake_client)
    try:
        spot = await gateway.get_spot("$SPX")
    finally:
        await fake_client.aclose()

    assert spot.spot.symbol == "$SPX"
    assert spot.spot.price == 6300.5
    assert captured_headers["x-internal-api-key"] == "test-key"


async def test_build_gateway_client_does_not_own_injected_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SPOT_PAYLOAD)

    transport = httpx.MockTransport(handler)
    fake_client = httpx.AsyncClient(base_url="https://gateway.internal", transport=transport)

    gateway = build_gateway_client(make_settings(), client=fake_client)
    await gateway.close()

    assert not fake_client.is_closed
    await fake_client.aclose()


async def test_minute_history_and_extended_session_history_contracts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/history":
            return httpx.Response(200, json=HISTORY_PAYLOAD)
        return httpx.Response(200, json=SESSION_HISTORY_PAYLOAD)

    fake_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    gateway = build_gateway_client(make_settings(), client=fake_client)
    try:
        history = await gateway.get_history("AAPL", frequency="minute", days_back=2)
        session = await gateway.get_session_history(
            "AAPL", dt.date(2026, 8, 25), session="extended"
        )
    finally:
        await fake_client.aclose()

    assert history.history.frequency == "minute"
    assert session.session_history.session == "extended"
    assert str(requests[0].url.params) == "symbol=AAPL&frequency=minute&days_back=2"
    assert str(requests[1].url.params) == "symbol=AAPL&date=2026-08-25&session=extended"


@pytest.mark.parametrize(
    ("status", "error_type", "expected_calls"),
    [
        (401, GatewayAuthenticationError, 1),
        (403, GatewayAuthorizationError, 1),
        (429, GatewayCapacityError, 5),
        (502, GatewayUnavailableError, 5),
        (503, GatewayUnavailableError, 5),
    ],
)
async def test_status_handling_and_transient_retry_policy(
    status: int,
    error_type: type[Exception],
    expected_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"code": "test", "message": "test"}})

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    fake_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    gateway = build_gateway_client(make_settings(), client=fake_client)
    try:
        with pytest.raises(error_type):
            await gateway.get_quotes(["AAPL"])
    finally:
        await fake_client.aclose()

    assert calls == expected_calls
    if expected_calls == 1:
        assert delays == []
    else:
        # One backoff sleep between each attempt: full-jitter exponential
        # backoff capped at the 8 s ceiling.
        caps = [0.5, 1.0, 2.0, 4.0]
        assert len(delays) == expected_calls - 1
        assert all(0.0 <= delay <= cap for delay, cap in zip(delays, caps))


async def test_timeout_retries_but_malformed_contract_fails_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout_calls = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal timeout_calls
        timeout_calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    timeout_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(timeout_handler)
    )
    gateway = build_gateway_client(make_settings(), client=timeout_client)
    try:
        with pytest.raises(GatewayTimeoutError):
            await gateway.get_quotes(["AAPL"])
    finally:
        await timeout_client.aclose()
    assert timeout_calls == 5

    malformed_calls = 0

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal malformed_calls
        malformed_calls += 1
        return httpx.Response(200, content=b"not-json")

    malformed_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(malformed_handler)
    )
    gateway = build_gateway_client(make_settings(), client=malformed_client)
    try:
        with pytest.raises(GatewayResponseError):
            await gateway.get_quotes(["AAPL"])
    finally:
        await malformed_client.aclose()
    assert malformed_calls == 1


async def test_gather_staggers_submission_and_bounds_concurrency() -> None:
    active = 0
    high_water = 0
    starts: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, high_water
        starts.append(asyncio.get_event_loop().time())
        active += 1
        high_water = max(high_water, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=SPOT_PAYLOAD)

    fake_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    settings = make_settings().model_copy(
        update={
            "gateway_max_concurrency": 3,
            "gateway_fan_out_stagger_seconds": 0.05,
        }
    )
    gateway = build_gateway_client(settings, client=fake_client)
    try:
        results = await gateway.gather(
            [lambda sym=sym: gateway.get_spot(sym) for sym in ["A", "B", "C", "D", "E"]]
        )
    finally:
        await fake_client.aclose()

    assert len(results) == 5
    assert high_water <= 3
    # The fifth submission is staggered well past the first.
    assert starts[-1] - starts[0] >= 0.05


async def test_backoff_is_jittered_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "x", "message": "x"}})

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    fake_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    settings = make_settings().model_copy(
        update={"gateway_max_attempts": 6, "gateway_retry_max_backoff_seconds": 3.0}
    )
    gateway = build_gateway_client(settings, client=fake_client)
    try:
        with pytest.raises(GatewayUnavailableError):
            await gateway.get_quotes(["AAPL"])
    finally:
        await fake_client.aclose()

    assert len(delays) == 5
    assert all(0.0 <= delay <= 3.0 for delay in delays)
    assert len(set(delays)) > 1  # jitter, not a fixed schedule


async def test_concurrency_is_bounded() -> None:
    active = 0
    high_water = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, high_water
        active += 1
        high_water = max(high_water, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=SPOT_PAYLOAD)

    fake_client = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    settings = make_settings().model_copy(update={"gateway_max_concurrency": 2})
    gateway = build_gateway_client(settings, client=fake_client)
    try:
        await asyncio.gather(*(gateway.get_spot("$SPX") for _ in range(6)))
    finally:
        await fake_client.aclose()

    assert high_water == 2

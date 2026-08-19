import datetime as dt

import httpx

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

"""Factory for the gateway market-data client. Import this instead of constructing
the SDK client inline."""

from __future__ import annotations

import httpx
from schwab_gateway_sdk import GatewayMarketDataClient

from afterhours_lab.config import AppSettings


def build_gateway_client(
    settings: AppSettings,
    *,
    client: httpx.AsyncClient | None = None,
) -> GatewayMarketDataClient:
    return GatewayMarketDataClient(
        base_url=settings.gateway_url,
        api_key=settings.gateway_api_key.get_secret_value(),
        client=client,
    )

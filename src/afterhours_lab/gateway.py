"""Bounded, retrying client for SchwabGateway's read-only market-data contract."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

import httpx
from schwab_gateway_sdk import GatewayMarketDataClient
from schwab_gateway_sdk.client import (
    GatewayCapacityError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)
from schwab_gateway_sdk.models import (
    HistoryResponseV1,
    QuoteResponseV1,
    SessionHistoryResponseV1,
)

from afterhours_lab.config import AppSettings

_T = TypeVar("_T")
_TRANSIENT_ERRORS = (GatewayCapacityError, GatewayTimeoutError, GatewayUnavailableError)


class BoundedGatewayClient:
    """App-level resilience around the official typed SDK.

    Authentication, authorization, and malformed-contract failures are deliberately
    not retried. Capacity, timeout, and upstream availability failures are retried
    with a short exponential backoff. The semaphore bounds attempts across every
    endpoint, which keeps this background-priority client from creating bursts.
    """

    def __init__(
        self,
        client: GatewayMarketDataClient,
        *,
        max_concurrency: int,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    async def _request(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._semaphore:
                    return await operation()
            except _TRANSIENT_ERRORS:
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(self._backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("retry loop exhausted")

    async def get_quotes(self, symbols: Sequence[str]) -> QuoteResponseV1:
        return await self._request(lambda: self._client.get_quotes(symbols))

    async def get_history(
        self, symbol: str, *, frequency: str = "minute", days_back: int | None = None
    ) -> HistoryResponseV1:
        return await self._request(
            lambda: self._client.get_history(
                symbol, frequency=frequency, days_back=days_back
            )
        )

    async def get_session_history(
        self, symbol: str, date: dt.date, *, session: str = "extended"
    ) -> SessionHistoryResponseV1:
        return await self._request(
            lambda: self._client.get_session_history(symbol, date, session=session)
        )

    async def get_spot(self, symbol: str):
        return await self._request(lambda: self._client.get_spot(symbol))

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> BoundedGatewayClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()


def build_gateway_client(
    settings: AppSettings,
    *,
    client: httpx.AsyncClient | None = None,
) -> BoundedGatewayClient:
    sdk_client = GatewayMarketDataClient(
        base_url=settings.gateway_url,
        api_key=settings.gateway_api_key.get_secret_value(),
        timeout_seconds=settings.gateway_timeout_seconds,
        client=client,
    )
    return BoundedGatewayClient(
        sdk_client,
        max_concurrency=settings.gateway_max_concurrency,
        max_attempts=settings.gateway_max_attempts,
        backoff_seconds=settings.gateway_retry_backoff_seconds,
    )

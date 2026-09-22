"""Bounded, retrying client for SchwabGateway's read-only market-data contract."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections.abc import Awaitable, Callable, Iterable, Sequence
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
        max_backoff_seconds: float = 8.0,
        stagger_seconds: float = 0.0,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._stagger_seconds = stagger_seconds

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff capped at the ceiling, with full jitter.

        The gateway sheds background work when its shared worker is busy; a
        synchronized retry storm just prolongs the saturation, so each client
        waits a random slice of the growing window instead.
        """
        ceiling = min(
            self._backoff_seconds * (2 ** (attempt - 1)), self._max_backoff_seconds
        )
        return random.uniform(0, ceiling)

    async def _request(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._semaphore:
                    return await operation()
            except _TRANSIENT_ERRORS:
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(self._backoff_delay(attempt))
        raise AssertionError("retry loop exhausted")

    async def gather(self, factories: Iterable[Callable[[], Awaitable[_T]]]) -> list[_T]:
        """Run a fan-out of requests, staggering submission so a scheduler tick
        does not fire a perfectly synchronized burst at the single upstream worker.

        Concurrency is still bounded by the shared semaphore; the stagger only
        smooths the leading edge.
        """

        async def _run(index: int, factory: Callable[[], Awaitable[_T]]) -> _T:
            if self._stagger_seconds:
                await asyncio.sleep(index * self._stagger_seconds)
            return await factory()

        return await asyncio.gather(
            *(_run(i, factory) for i, factory in enumerate(factories))
        )

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
        max_backoff_seconds=settings.gateway_retry_max_backoff_seconds,
        stagger_seconds=settings.gateway_fan_out_stagger_seconds,
    )

"""Earnings calendar client. Source: Finnhub's free /calendar/earnings endpoint.

This is independent of the SchwabGateway connection — earnings-calendar data isn't
market data and doesn't exist on the gateway's contract. Requires its own API key.
"""

from __future__ import annotations

import datetime as dt

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


class EarningsSettings(BaseSettings):
    """Credentials for the earnings-calendar source. Separate from gateway auth."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    finnhub_api_key: SecretStr = Field(validation_alias="FINNHUB_API_KEY")


class EarningsEntry(BaseModel):
    """One earnings-calendar row. Extra fields from the upstream API are ignored;
    this source isn't a versioned contract like the gateway's.

    EPS/revenue estimate-vs-actual is the biggest explanatory variable for a
    post-earnings price reaction, so those fields (plus quarter/year, needed to
    disambiguate which print a surprise belongs to) are captured alongside the
    date/hour used for scheduling capture windows."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    symbol: str
    date: dt.date
    hour: str | None = None
    eps_estimate: float | None = Field(default=None, alias="epsEstimate")
    eps_actual: float | None = Field(default=None, alias="epsActual")
    revenue_estimate: float | None = Field(default=None, alias="revenueEstimate")
    revenue_actual: float | None = Field(default=None, alias="revenueActual")
    quarter: int | None = None
    year: int | None = None

    @property
    def is_after_close(self) -> bool:
        return self.hour == "amc"


class EarningsCalendarError(RuntimeError):
    """Raised on any transport or response failure from the earnings source."""


class EarningsCalendarClient:
    """Thin client for Finnhub's earnings calendar. Free tier: 60 calls/min."""

    def __init__(
        self,
        settings: EarningsSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = settings.finnhub_api_key.get_secret_value()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=FINNHUB_BASE_URL, timeout=10.0)

    async def get_earnings_calendar(
        self, from_date: dt.date, to_date: dt.date
    ) -> list[EarningsEntry]:
        try:
            response = await self._client.get(
                "/calendar/earnings",
                params={
                    "from": from_date.isoformat(),
                    "to": to_date.isoformat(),
                    "token": self._api_key,
                },
            )
        except httpx.TimeoutException as exc:
            raise EarningsCalendarError("earnings calendar request timed out") from exc
        except httpx.TransportError as exc:
            raise EarningsCalendarError("earnings calendar request unavailable") from exc

        if response.status_code != 200:
            raise EarningsCalendarError(
                f"earnings calendar request failed with status {response.status_code}"
            )
        try:
            payload = response.json()
            return [EarningsEntry.model_validate(row) for row in payload["earningsCalendar"]]
        except (KeyError, ValueError) as exc:
            raise EarningsCalendarError("earnings calendar returned an unexpected payload") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> EarningsCalendarClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()


def after_close_symbols(entries: list[EarningsEntry]) -> list[str]:
    """Distinct symbols reporting after market close, in first-seen order."""
    seen: dict[str, None] = {}
    for entry in entries:
        if entry.is_after_close:
            seen.setdefault(entry.symbol, None)
    return list(seen)


def after_close_entries(entries: list[EarningsEntry]) -> list[EarningsEntry]:
    """Distinct (symbol, date) after-close entries, in first-seen order."""
    seen: dict[tuple[str, dt.date], EarningsEntry] = {}
    for entry in entries:
        if entry.is_after_close:
            seen.setdefault((entry.symbol, entry.date), entry)
    return list(seen.values())

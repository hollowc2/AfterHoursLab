"""Smoke entry point: fetch one spot quote through the gateway and print it."""

from __future__ import annotations

import asyncio
import sys

import structlog
from schwab_gateway_sdk.client import GatewayClientError

from afterhours_lab.config import AppSettings
from afterhours_lab.gateway import build_gateway_client

log = structlog.get_logger()

SMOKE_SYMBOL = "$SPX"


async def run_smoke() -> int:
    settings = AppSettings()
    async with build_gateway_client(settings) as gateway:
        try:
            spot = await gateway.get_spot(SMOKE_SYMBOL)
        except GatewayClientError as exc:
            log.error("gateway_smoke_failed", symbol=SMOKE_SYMBOL, error=str(exc))
            return 1
    print(spot.model_dump_json(indent=2))
    return 0


def main() -> None:
    sys.exit(asyncio.run(run_smoke()))


if __name__ == "__main__":
    main()

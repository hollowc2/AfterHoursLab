"""Lossless persistence adapters for the gateway's versioned response models."""

from __future__ import annotations

import datetime as dt

from schwab_gateway_sdk.models import (
    HistoryResponseV1,
    QuoteResponseV1,
    SessionHistoryResponseV1,
)

BAR_COLLECTION_MODES = {
    "legacy_unspecified",
    "manual_collection",
    "scheduled_capture",
    "historical_backfill",
}


async def preserve_quotes(
    conn,
    response: QuoteResponseV1,
    *,
    capture_window: str,
    earnings_date: dt.date,
) -> None:
    rows = [
        (
            quote.symbol,
            quote.event_timestamp,
            quote.gateway_received_at,
            quote.session,
            capture_window,
            earnings_date,
            quote.bid,
            quote.ask,
            quote.bid_size,
            quote.ask_size,
            quote.last,
            quote.last_size,
            quote.mark,
            quote.volume,
            quote.close,
            quote.net_percent_change,
            quote.source,
            quote.stale,
            quote.age_seconds,
            list(quote.data_quality_flags),
            response.schema_version,
            None,
            None,
            None,
            None,
        )
        for quote in response.quotes
    ]
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO quote_evidence (
            symbol, event_timestamp, gateway_received_at, session,
            capture_window, earnings_date, bid, ask, bid_size, ask_size,
            last, last_size, mark, volume, close, net_percent_change,
            source, stale, age_seconds, data_quality_flags, schema_version,
            exto_eligible, exchange_status,
            trading_status, session_eligible
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
            $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25
        )
        ON CONFLICT (symbol, gateway_received_at, capture_window, earnings_date)
        DO NOTHING
        """,
        rows,
    )


async def preserve_minute_history(
    conn,
    response: HistoryResponseV1,
    *,
    collection_mode: str = "manual_collection",
) -> None:
    history = response.history
    if history.frequency != "minute":
        raise ValueError("only minute gateway history can be preserved as bar evidence")
    await _preserve_bars(
        conn,
        symbol=history.symbol,
        bars=history.bars,
        session="unknown",
        event_timestamp=history.event_timestamp,
        gateway_received_at=history.gateway_received_at,
        source=history.source,
        endpoint="/v1/history",
        frequency=history.frequency,
        stale=history.stale,
        age_seconds=history.age_seconds,
        data_quality_flags=history.data_quality_flags,
        schema_version=response.schema_version,
        collection_mode=collection_mode,
    )


async def preserve_session_history(
    conn,
    response: SessionHistoryResponseV1,
    *,
    collection_mode: str = "manual_collection",
) -> None:
    history = response.session_history
    await _preserve_bars(
        conn,
        symbol=history.symbol,
        bars=history.candles,
        session=history.session,
        event_timestamp=history.event_timestamp,
        gateway_received_at=history.gateway_received_at,
        source=history.source,
        endpoint="/v1/session-history",
        frequency="minute",
        stale=history.stale,
        age_seconds=history.age_seconds,
        data_quality_flags=history.data_quality_flags,
        schema_version=response.schema_version,
        evidence_date=history.date,
        collection_mode=collection_mode,
    )


async def _preserve_bars(
    conn,
    *,
    symbol: str,
    bars,
    session: str,
    event_timestamp: dt.datetime | None,
    gateway_received_at: dt.datetime,
    source: str,
    endpoint: str,
    frequency: str,
    stale: bool,
    age_seconds: float | None,
    data_quality_flags: tuple[str, ...],
    schema_version: str,
    collection_mode: str,
    evidence_date: dt.date | None = None,
) -> None:
    if collection_mode not in BAR_COLLECTION_MODES:
        raise ValueError(f"unsupported bar collection mode: {collection_mode}")
    rows = [
        (
            symbol,
            bar.timestamp,
            session,
            evidence_date or bar.timestamp.date(),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            event_timestamp,
            gateway_received_at,
            source,
            endpoint,
            frequency,
            stale,
            age_seconds,
            list(data_quality_flags),
            schema_version,
            collection_mode,
            None,
            None,
            None,
            None,
        )
        for bar in bars
    ]
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO bar_evidence (
            symbol, ts, session, evidence_date, open, high, low, close, volume,
            gateway_event_timestamp, gateway_received_at, source, gateway_endpoint,
            frequency, stale, age_seconds, data_quality_flags, schema_version,
            collection_mode, exto_eligible, exchange_status, trading_status,
            session_eligible
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
            $14, $15, $16, $17, $18, $19, $20, $21, $22, $23
        )
        ON CONFLICT (
            symbol, ts, session, gateway_endpoint, gateway_received_at
        ) DO NOTHING
        """,
        rows,
    )

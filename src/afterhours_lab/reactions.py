"""Retrospective, report-only analytics for after-close earnings reactions.

The calculation deliberately reads the exact ``bar_evidence`` retrieval named by
``earnings_ohlcv_coverage``.  It never calls the market-data gateway and never
persists derived values, so rerunning it against unchanged evidence is reproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import math
import re
import sys
from collections.abc import Iterable, Sequence
from enum import StrEnum
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table

from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool

REACTION_THRESHOLD_PCT = 2.0
DELAYED_AFTER_MINUTES = 15
MIN_POSTMARKET_BARS = 5
HORIZONS_MINUTES = (1, 5, 15, 30, 60, 105)
DETECTOR_VERSION = "fixed-preclose-2pct-v1"
CLASSIFIER_VERSION = "path-retention-v1"
EASTERN = ZoneInfo("America/New_York")
FATAL_QUALITY_FLAGS = {
    "duplicate_minute_bars",
    "no_bars_in_requested_phase",
    "no_bars_returned",
}


class PathClassification(StrEnum):
    CONTINUATION = "immediate_continuation"
    FADE = "spike_and_fade"
    DELAYED = "delayed_breakout"
    WHIPSAW = "whipsaw"
    NO_MEANINGFUL = "no_trigger_in_window"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclasses.dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None


@dataclasses.dataclass(frozen=True)
class EventEvidence:
    symbol: str
    earnings_date: dt.date
    regular_covered: bool
    postmarket_covered: bool
    postmarket_expected_start: dt.datetime | None
    postmarket_expected_end: dt.datetime | None
    regular_expected_start: dt.datetime | None
    regular_expected_end: dt.datetime | None
    regular_bars: tuple[Bar, ...]
    postmarket_bars: tuple[Bar, ...]
    data_quality_flags: tuple[str, ...] = ()
    regular_collection_mode: str | None = None
    postmarket_collection_mode: str | None = None
    regular_response_sha256: str | None = None
    postmarket_response_sha256: str | None = None
    regular_stale: bool = False
    postmarket_stale: bool = False


@dataclasses.dataclass(frozen=True)
class ReactionFeatures:
    symbol: str
    earnings_date: dt.date
    classification: PathClassification
    pre_close: float | None
    reaction_timestamp: dt.datetime | None
    detection_delay_minutes: int | None
    initial_return_pct: float | None
    returns_pct: dict[int, float | None]
    cumulative_volume: dict[int, int | None]
    reaction_vwap_proxy: float | None
    volume_available: bool
    max_favorable_excursion_pct: float | None
    max_adverse_excursion_pct: float | None
    retracement_pct: float | None
    window_high_return_pct: float | None
    window_low_return_pct: float | None
    analyzed_through: dt.datetime | None
    study_observed_minutes: int
    study_missing_minutes: int
    study_coverage_ratio: float
    data_quality_flags: tuple[str, ...]
    detector_version: str = DETECTOR_VERSION
    classifier_version: str = CLASSIFIER_VERSION
    regular_collection_mode: str | None = None
    postmarket_collection_mode: str | None = None
    regular_response_sha256: str | None = None
    postmarket_response_sha256: str | None = None
    regular_stale: bool = False
    postmarket_stale: bool = False
    reason: str | None = None


def _pct_change(before: float, after: float) -> float:
    return (after - before) / before * 100.0


def _vwap_proxy(bars: Iterable[Bar]) -> float | None:
    volume_bars = [bar for bar in bars if bar.volume is not None and bar.volume > 0]
    total_volume = sum(bar.volume or 0 for bar in volume_bars)
    if total_volume <= 0:
        return None
    return sum(
        ((bar.high + bar.low + bar.close) / 3.0) * (bar.volume or 0)
        for bar in volume_bars
    ) / total_volume


def _bar_from_row(row) -> Bar:
    return Bar(
        ts=row["ts"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]) if row["volume"] is not None else None,
    )


def _deduplicate_bars(bars: Iterable[Bar]) -> tuple[Bar, ...]:
    """Sort bars and keep the first value for a duplicated timestamp.

    Coverage should point to one response, hence duplicates are unexpected.  Keeping
    the first result makes the fallback deterministic while the duplicate is also
    disclosed through the computed quality flags.
    """
    by_timestamp: dict[dt.datetime, Bar] = {}
    for bar in sorted(bars, key=lambda item: item.ts):
        by_timestamp.setdefault(bar.ts, bar)
    return tuple(by_timestamp.values())


def _study_coverage(event: EventEvidence) -> tuple[int, int, float]:
    expected = max(HORIZONS_MINUTES)
    if event.postmarket_expected_start is None:
        return 0, expected, 0.0
    end = event.postmarket_expected_start + dt.timedelta(minutes=expected)
    observed = len(
        {
            bar.ts
            for bar in event.postmarket_bars
            if event.postmarket_expected_start <= bar.ts < end
        }
    )
    observed = min(observed, expected)
    return observed, expected - observed, observed / expected


def _invalid_bar_reason(bars: Iterable[Bar], phase: str) -> str | None:
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            return f"{phase} contains non-finite or non-positive OHLC"
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            return f"{phase} contains invalid OHLC bounds"
        if bar.high < bar.low:
            return f"{phase} contains high below low"
        if bar.volume is None or bar.volume < 0:
            return f"{phase} contains absent or negative volume"
        if bar.ts.tzinfo is None or bar.ts.utcoffset() is None:
            return f"{phase} contains a timezone-naive timestamp"
    return None


def _empty_features(event: EventEvidence, reason: str) -> ReactionFeatures:
    observed, missing, ratio = _study_coverage(event)
    return ReactionFeatures(
        symbol=event.symbol,
        earnings_date=event.earnings_date,
        classification=PathClassification.INSUFFICIENT_DATA,
        pre_close=None,
        reaction_timestamp=None,
        detection_delay_minutes=None,
        initial_return_pct=None,
        returns_pct={minutes: None for minutes in HORIZONS_MINUTES},
        cumulative_volume={minutes: None for minutes in HORIZONS_MINUTES},
        reaction_vwap_proxy=None,
        volume_available=False,
        max_favorable_excursion_pct=None,
        max_adverse_excursion_pct=None,
        retracement_pct=None,
        window_high_return_pct=None,
        window_low_return_pct=None,
        analyzed_through=None,
        study_observed_minutes=observed,
        study_missing_minutes=missing,
        study_coverage_ratio=ratio,
        data_quality_flags=event.data_quality_flags,
        regular_collection_mode=event.regular_collection_mode,
        postmarket_collection_mode=event.postmarket_collection_mode,
        regular_response_sha256=event.regular_response_sha256,
        postmarket_response_sha256=event.postmarket_response_sha256,
        regular_stale=event.regular_stale,
        postmarket_stale=event.postmarket_stale,
        reason=reason,
    )


def compute_reaction_features(event: EventEvidence) -> ReactionFeatures:
    """Compute one event using fixed, documented thresholds.

    Reaction is the first postmarket candle whose close is at least 2% away from the
    last regular-session close. Its timestamp is the end of that interval (start +
    one minute), when the close-confirmed signal becomes knowable. Fixed horizon
    returns remain relative to pre-close.
    Because evidence timestamps identify interval starts, 1m uses the 16:00 bar close,
    5m uses 16:04, and 105m uses 17:44. Missing minutes are never interpolated.

    Classification precedence is insufficient, no-trigger, whipsaw, fade, delayed,
    then continuation. A whipsaw reaches 2% on both sides of pre-close. A fade retains
    less than half its maximum direction-adjusted excursion at 17:45 ET. Delayed means
    detection occurs 15 or more minutes after the covered postmarket phase begins.
    """
    if not event.regular_covered or not event.postmarket_covered:
        return _empty_features(event, "missing authoritative regular or postmarket coverage")

    bounds = (
        event.regular_expected_start,
        event.regular_expected_end,
        event.postmarket_expected_start,
        event.postmarket_expected_end,
    )
    if any(bound is None for bound in bounds):
        return _empty_features(event, "coverage is missing expected phase bounds")
    if any(bound.tzinfo is None or bound.utcoffset() is None for bound in bounds if bound):
        return _empty_features(event, "coverage contains a timezone-naive phase bound")
    assert event.regular_expected_start is not None
    assert event.regular_expected_end is not None
    assert event.postmarket_expected_start is not None
    assert event.postmarket_expected_end is not None
    if (
        event.regular_expected_start >= event.regular_expected_end
        or event.postmarket_expected_start >= event.postmarket_expected_end
        or event.regular_expected_end != event.postmarket_expected_start
    ):
        return _empty_features(event, "coverage contains invalid phase bounds")
    if event.regular_stale or event.postmarket_stale:
        return _empty_features(event, "canonical regular or postmarket response is stale")
    fatal_flags = sorted(FATAL_QUALITY_FLAGS.intersection(event.data_quality_flags))
    if fatal_flags:
        return _empty_features(event, f"fatal coverage quality flag(s): {','.join(fatal_flags)}")
    if event.postmarket_expected_start.astimezone(EASTERN).time() != dt.time(16, 0):
        return _empty_features(event, "early-close session excluded from the 16:00-17:45 ET study")

    invalid_reason = _invalid_bar_reason(event.regular_bars, "regular evidence")
    invalid_reason = invalid_reason or _invalid_bar_reason(
        event.postmarket_bars, "postmarket evidence"
    )
    if invalid_reason:
        return _empty_features(event, invalid_reason)
    if any(
        not event.regular_expected_start <= bar.ts < event.regular_expected_end
        for bar in event.regular_bars
    ):
        return _empty_features(event, "regular bar falls outside declared coverage bounds")
    if any(
        not event.postmarket_expected_start <= bar.ts < event.postmarket_expected_end
        for bar in event.postmarket_bars
    ):
        return _empty_features(event, "postmarket bar falls outside declared coverage bounds")
    regular = _deduplicate_bars(event.regular_bars)
    all_postmarket = _deduplicate_bars(event.postmarket_bars)
    if len(regular) != len(event.regular_bars) or len(all_postmarket) != len(
        event.postmarket_bars
    ):
        return _empty_features(event, "duplicate bar timestamp in canonical evidence")
    study_end_exclusive = event.postmarket_expected_start + dt.timedelta(
        minutes=max(HORIZONS_MINUTES)
    )
    if study_end_exclusive > event.postmarket_expected_end:
        return _empty_features(event, "postmarket coverage ends before the study window")
    postmarket = tuple(
        bar
        for bar in all_postmarket
        if event.postmarket_expected_start <= bar.ts < study_end_exclusive
    )
    if not regular:
        return _empty_features(event, "no regular-session bars in authoritative coverage")
    if len(postmarket) < MIN_POSTMARKET_BARS:
        return _empty_features(event, f"fewer than {MIN_POSTMARKET_BARS} postmarket bars")

    expected_preclose_timestamp = event.regular_expected_end - dt.timedelta(minutes=1)
    preclose_bar = next(
        (bar for bar in regular if bar.ts == expected_preclose_timestamp),
        None,
    )
    if preclose_bar is None:
        return _empty_features(event, "exact final regular-session minute is missing")
    pre_close = preclose_bar.close
    if not math.isfinite(pre_close) or pre_close <= 0:
        return _empty_features(event, "pre-close is absent, non-finite, or non-positive")

    flags = list(event.data_quality_flags)

    by_timestamp = {bar.ts: bar for bar in postmarket}
    horizon_targets = {
        minutes: event.postmarket_expected_start + dt.timedelta(minutes=minutes - 1)
        for minutes in HORIZONS_MINUTES
    }
    horizon_returns = {
        minutes: (
            _pct_change(pre_close, by_timestamp[target].close)
            if (target := horizon_targets[minutes]) in by_timestamp
            else None
        )
        for minutes in HORIZONS_MINUTES
    }
    cumulative_volume = {
        minutes: sum(
            bar.volume or 0 for bar in postmarket if bar.ts <= horizon_targets[minutes]
        )
        for minutes in HORIZONS_MINUTES
    }
    vwap = _vwap_proxy(postmarket)
    if vwap is None and "volume_unavailable_for_vwap" not in flags:
        flags.append("volume_unavailable_for_vwap")

    window_high = max(_pct_change(pre_close, bar.high) for bar in postmarket)
    window_low = min(_pct_change(pre_close, bar.low) for bar in postmarket)
    missing_checkpoints = [
        minutes for minutes, value in horizon_returns.items() if value is None
    ]
    if missing_checkpoints or vwap is None:
        reasons = []
        if missing_checkpoints:
            joined = ",".join(str(minutes) for minutes in missing_checkpoints)
            reasons.append(f"missing exact checkpoint bar(s): {joined}m")
        if vwap is None:
            reasons.append("zero eligible volume in study window")
        incomplete = _empty_features(event, "; ".join(reasons))
        return dataclasses.replace(
            incomplete,
            pre_close=pre_close,
            returns_pct=horizon_returns,
            cumulative_volume=cumulative_volume,
            reaction_vwap_proxy=vwap,
            volume_available=vwap is not None,
            analyzed_through=postmarket[-1].ts,
            window_high_return_pct=window_high,
            window_low_return_pct=window_low,
            data_quality_flags=tuple(flags),
        )

    close_returns = [_pct_change(pre_close, bar.close) for bar in postmarket]
    reaction_index = next(
        (
            index
            for index, value in enumerate(close_returns)
            if abs(value) >= REACTION_THRESHOLD_PCT
        ),
        None,
    )
    if reaction_index is None:
        return ReactionFeatures(
            symbol=event.symbol,
            earnings_date=event.earnings_date,
            classification=PathClassification.NO_MEANINGFUL,
            pre_close=pre_close,
            reaction_timestamp=None,
            detection_delay_minutes=None,
            initial_return_pct=None,
            returns_pct=horizon_returns,
            cumulative_volume=cumulative_volume,
            reaction_vwap_proxy=vwap,
            volume_available=vwap is not None,
            max_favorable_excursion_pct=None,
            max_adverse_excursion_pct=None,
            retracement_pct=None,
            window_high_return_pct=window_high,
            window_low_return_pct=window_low,
            analyzed_through=postmarket[-1].ts,
            study_observed_minutes=len(postmarket),
            study_missing_minutes=max(HORIZONS_MINUTES) - len(postmarket),
            study_coverage_ratio=len(postmarket) / max(HORIZONS_MINUTES),
            data_quality_flags=tuple(flags),
            regular_collection_mode=event.regular_collection_mode,
            postmarket_collection_mode=event.postmarket_collection_mode,
            regular_response_sha256=event.regular_response_sha256,
            postmarket_response_sha256=event.postmarket_response_sha256,
            regular_stale=event.regular_stale,
            postmarket_stale=event.postmarket_stale,
            reason=f"no_trigger_in_window: no close moved {REACTION_THRESHOLD_PCT:.1f}%",
        )

    reaction_bar = postmarket[reaction_index]
    signal_timestamp = reaction_bar.ts + dt.timedelta(minutes=1)
    initial_return = close_returns[reaction_index]
    direction = 1.0 if initial_return > 0 else -1.0
    path = postmarket[reaction_index + 1 :]
    if not path:
        incomplete = _empty_features(event, "no post-signal bar before the study cutoff")
        return dataclasses.replace(
            incomplete,
            pre_close=pre_close,
            returns_pct=horizon_returns,
            cumulative_volume=cumulative_volume,
            reaction_vwap_proxy=vwap,
            volume_available=True,
            analyzed_through=postmarket[-1].ts,
            window_high_return_pct=window_high,
            window_low_return_pct=window_low,
            data_quality_flags=tuple(flags),
        )

    directional_highs: list[float] = []
    directional_lows: list[float] = []
    for bar in path:
        high_return = _pct_change(pre_close, bar.high)
        low_return = _pct_change(pre_close, bar.low)
        directional_highs.append(max(direction * high_return, direction * low_return))
        directional_lows.append(min(direction * high_return, direction * low_return))
    # The trigger candle's high/low are excluded because they occurred before the
    # close-confirmed signal was available. Its close is known at signal time, so it
    # is the causal starting point for post-signal excursion and retracement.
    starting_directional_return = direction * initial_return
    max_favorable = max(0.0, starting_directional_return, max(directional_highs))
    max_adverse = min(0.0, starting_directional_return, min(directional_lows))
    terminal_directional_return = direction * _pct_change(pre_close, path[-1].close)
    retracement = (
        max(0.0, (max_favorable - terminal_directional_return) / max_favorable * 100.0)
        if max_favorable > 0
        else None
    )

    post_signal_returns = close_returns[reaction_index + 1 :]
    positive_extreme = max(initial_return, *post_signal_returns)
    negative_extreme = min(initial_return, *post_signal_returns)
    whipsaw = (
        positive_extreme >= REACTION_THRESHOLD_PCT
        and negative_extreme <= -REACTION_THRESHOLD_PCT
    )
    phase_start = event.postmarket_expected_start
    detection_delay = max(0, int((signal_timestamp - phase_start).total_seconds() // 60))

    if whipsaw:
        classification = PathClassification.WHIPSAW
    elif retracement is not None and retracement > 50.0:
        classification = PathClassification.FADE
    elif detection_delay >= DELAYED_AFTER_MINUTES:
        classification = PathClassification.DELAYED
    else:
        classification = PathClassification.CONTINUATION

    return ReactionFeatures(
        symbol=event.symbol,
        earnings_date=event.earnings_date,
        classification=classification,
        pre_close=pre_close,
        reaction_timestamp=signal_timestamp,
        detection_delay_minutes=detection_delay,
        initial_return_pct=initial_return,
        returns_pct=horizon_returns,
        cumulative_volume=cumulative_volume,
        reaction_vwap_proxy=vwap,
        volume_available=vwap is not None,
        max_favorable_excursion_pct=max_favorable,
        max_adverse_excursion_pct=max_adverse,
        retracement_pct=retracement,
        window_high_return_pct=window_high,
        window_low_return_pct=window_low,
        analyzed_through=path[-1].ts,
        study_observed_minutes=len(postmarket),
        study_missing_minutes=max(HORIZONS_MINUTES) - len(postmarket),
        study_coverage_ratio=len(postmarket) / max(HORIZONS_MINUTES),
        data_quality_flags=tuple(flags),
        regular_collection_mode=event.regular_collection_mode,
        postmarket_collection_mode=event.postmarket_collection_mode,
        regular_response_sha256=event.regular_response_sha256,
        postmarket_response_sha256=event.postmarket_response_sha256,
        regular_stale=event.regular_stale,
        postmarket_stale=event.postmarket_stale,
    )


async def fetch_event_evidence(
    conn,
    *,
    from_date: dt.date,
    to_date: dt.date,
    symbols: Sequence[str] = (),
) -> list[EventEvidence]:
    """Load bars only from the response identified by each authoritative coverage row."""
    requested = list(symbols)
    event_rows = await conn.fetch(
        """
        SELECT e.symbol, e.earnings_date,
               regular.expected_start AS regular_expected_start,
               regular.expected_end AS regular_expected_end,
               post.expected_start AS post_expected_start,
               post.expected_end AS post_expected_end,
               regular.gateway_received_at AS regular_received_at,
               post.gateway_received_at AS post_received_at,
               regular.source AS regular_source,
               post.source AS post_source,
               regular.market_date AS regular_market_date,
               post.market_date AS post_market_date,
               regular.collection_mode AS regular_collection_mode,
               post.collection_mode AS post_collection_mode,
               regular.response_sha256 AS regular_response_sha256,
               post.response_sha256 AS post_response_sha256,
               COALESCE(regular.data_quality_flags, ARRAY[]::text[])
                 || COALESCE(post.data_quality_flags, ARRAY[]::text[]) AS quality_flags
        FROM earnings_events e
        LEFT JOIN earnings_ohlcv_coverage regular
          ON regular.symbol=e.symbol AND regular.earnings_date=e.earnings_date
         AND regular.phase='earnings_regular'
        LEFT JOIN earnings_ohlcv_coverage post
          ON post.symbol=e.symbol AND post.earnings_date=e.earnings_date
         AND post.phase='earnings_postmarket'
        WHERE e.hour='amc'
          AND e.earnings_date BETWEEN $1 AND $2
          AND (cardinality($3::text[]) = 0 OR e.symbol=ANY($3::text[]))
        ORDER BY e.earnings_date, e.symbol
        """,
        from_date,
        to_date,
        requested,
    )

    events: list[EventEvidence] = []
    for row in event_rows:
        regular_bars: tuple[Bar, ...] = ()
        postmarket_bars: tuple[Bar, ...] = ()
        regular_stale = False
        postmarket_stale = False
        if row["regular_received_at"] is not None:
            bars = await conn.fetch(
                """
                SELECT ts, open, high, low, close, volume, stale
                FROM bar_evidence
                WHERE symbol=$1 AND session='regular' AND evidence_date=$2
                  AND gateway_endpoint='/v1/session-history'
                  AND gateway_received_at=$3 AND source=$4
                  AND ts >= $5 AND ts < $6
                ORDER BY ts, id
                """,
                row["symbol"],
                row["regular_market_date"],
                row["regular_received_at"],
                row["regular_source"],
                row["regular_expected_start"],
                row["regular_expected_end"],
            )
            regular_bars = tuple(_bar_from_row(bar) for bar in bars)
            regular_stale = any(bar["stale"] for bar in bars)
        if row["post_received_at"] is not None:
            bars = await conn.fetch(
                """
                SELECT ts, open, high, low, close, volume, stale
                FROM bar_evidence
                WHERE symbol=$1 AND session='extended' AND evidence_date=$2
                  AND gateway_endpoint='/v1/session-history'
                  AND gateway_received_at=$3 AND source=$4
                  AND ts >= $5 AND ts < $6
                ORDER BY ts, id
                """,
                row["symbol"],
                row["post_market_date"],
                row["post_received_at"],
                row["post_source"],
                row["post_expected_start"],
                row["post_expected_end"],
            )
            postmarket_bars = tuple(_bar_from_row(bar) for bar in bars)
            postmarket_stale = any(bar["stale"] for bar in bars)
        events.append(
            EventEvidence(
                symbol=row["symbol"],
                earnings_date=row["earnings_date"],
                regular_covered=row["regular_received_at"] is not None,
                postmarket_covered=row["post_received_at"] is not None,
                postmarket_expected_start=row["post_expected_start"],
                postmarket_expected_end=row["post_expected_end"],
                regular_expected_start=row["regular_expected_start"],
                regular_expected_end=row["regular_expected_end"],
                regular_bars=regular_bars,
                postmarket_bars=postmarket_bars,
                data_quality_flags=tuple(dict.fromkeys(row["quality_flags"] or ())),
                regular_collection_mode=row["regular_collection_mode"],
                postmarket_collection_mode=row["post_collection_mode"],
                regular_response_sha256=row["regular_response_sha256"],
                postmarket_response_sha256=row["post_response_sha256"],
                regular_stale=regular_stale,
                postmarket_stale=postmarket_stale,
            )
        )
    return events


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def render_table(results: Sequence[ReactionFeatures]) -> Table:
    table = Table(title="AfterHoursLab — retrospective earnings reactions")
    columns = (
        "Symbol", "Date", "Class", "Signal available", "Initial", "PM+1", "PM+5",
        "PM+15", "PM+30", "PM+60", "PM+105", "PM Vol105", "Coverage", "VWAP proxy",
        "MFE", "MAE", "Retrace", "Quality / reason",
    )
    for column in columns:
        table.add_column(column)
    for result in results:
        detail_parts = list(result.data_quality_flags)
        if result.reason:
            detail_parts.append(result.reason)
        detail = ",".join(detail_parts) or "-"
        table.add_row(
            result.symbol,
            result.earnings_date.isoformat(),
            result.classification.value,
            result.reaction_timestamp.isoformat() if result.reaction_timestamp else "-",
            _fmt_pct(result.initial_return_pct),
            _fmt_pct(result.returns_pct[1]),
            _fmt_pct(result.returns_pct[5]),
            _fmt_pct(result.returns_pct[15]),
            _fmt_pct(result.returns_pct[30]),
            _fmt_pct(result.returns_pct[60]),
            _fmt_pct(result.returns_pct[105]),
            str(result.cumulative_volume[105] or 0),
            f"{result.study_observed_minutes}/{max(HORIZONS_MINUTES)}",
            (
                f"{result.reaction_vwap_proxy:.4f}"
                if result.reaction_vwap_proxy is not None
                else "unavailable"
            ),
            _fmt_pct(result.max_favorable_excursion_pct),
            _fmt_pct(result.max_adverse_excursion_pct),
            _fmt_pct(result.retracement_pct),
            detail,
        )
    return table


def _jsonable(result: ReactionFeatures) -> dict:
    payload = dataclasses.asdict(result)
    payload["classification"] = result.classification.value
    payload["earnings_date"] = result.earnings_date.isoformat()
    payload["reaction_timestamp"] = (
        result.reaction_timestamp.isoformat() if result.reaction_timestamp else None
    )
    payload["analyzed_through"] = (
        result.analyzed_through.isoformat() if result.analyzed_through else None
    )
    payload["returns_pct"] = {str(key): value for key, value in result.returns_pct.items()}
    payload["cumulative_volume"] = {
        str(key): value for key, value in result.cumulative_volume.items()
    }
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report deterministic reactions from authoritative earnings OHLCV evidence"
    )
    parser.add_argument("--from", dest="from_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--symbol", action="append", default=[], help="repeat to limit symbols")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must be on or before --to")
    normalized: list[str] = []
    for raw_symbol in args.symbol:
        symbol = raw_symbol.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
            parser.error(f"invalid equity symbol: {raw_symbol}")
        if symbol not in normalized:
            normalized.append(symbol)
    args.symbol = normalized
    return args


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            evidence = await fetch_event_evidence(
                conn,
                from_date=args.from_date,
                to_date=args.to_date,
                symbols=args.symbol,
            )
    finally:
        await pool.close()

    results = [compute_reaction_features(event) for event in evidence]
    if args.json:
        print(json.dumps([_jsonable(result) for result in results], indent=2, sort_keys=True))
    elif results:
        Console().print(render_table(results))
        print(
            f"Detection threshold: {REACTION_THRESHOLD_PCT:.1f}% from pre-close; "
            "signals are close-confirmed at interval end; horizons are fixed interval "
            "closes from 16:00 through 17:45 ET and "
            f"are never interpolated. detector={DETECTOR_VERSION}; "
            f"classifier={CLASSIFIER_VERSION}."
        )
    else:
        print("no after-close earnings events found")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()

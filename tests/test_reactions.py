from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest

from afterhours_lab.reactions import (
    Bar,
    EventEvidence,
    PathClassification,
    compute_reaction_features,
    fetch_event_evidence,
)

EASTERN = ZoneInfo("America/New_York")
DATE = dt.date(2026, 8, 20)
START = dt.datetime.combine(DATE, dt.time(16), EASTERN)


def _event(
    close_at: Callable[[int], float],
    *,
    missing_minutes: set[int] | None = None,
    volume: int = 10,
) -> EventEvidence:
    missing_minutes = missing_minutes or set()
    postmarket = tuple(
        Bar(
            ts=START + dt.timedelta(minutes=minute),
            open=close_at(minute),
            high=close_at(minute) + 0.1,
            low=close_at(minute) - 0.1,
            close=close_at(minute),
            volume=volume,
        )
        for minute in range(105)
        if minute not in missing_minutes
    )
    return EventEvidence(
        symbol="TEST",
        earnings_date=DATE,
        regular_covered=True,
        postmarket_covered=True,
        postmarket_expected_start=START,
        postmarket_expected_end=START + dt.timedelta(hours=4),
        regular_expected_start=START - dt.timedelta(hours=6, minutes=30),
        regular_expected_end=START,
        regular_bars=(
            Bar(
                ts=START - dt.timedelta(minutes=1),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=100,
            ),
        ),
        postmarket_bars=postmarket,
    )


def test_fixed_horizons_use_interval_start_convention() -> None:
    result = compute_reaction_features(_event(lambda minute: 103.0 + minute / 100.0))

    assert result.classification == PathClassification.CONTINUATION
    assert result.reaction_timestamp == START + dt.timedelta(minutes=1)
    assert result.returns_pct[1] == pytest.approx(3.0)
    assert result.returns_pct[5] == pytest.approx(3.04)
    assert result.returns_pct[105] == pytest.approx(4.04)
    assert result.cumulative_volume[1] == 10
    assert result.cumulative_volume[105] == 1050
    assert result.reaction_vwap_proxy is not None
    assert result.study_observed_minutes == 105
    assert result.study_missing_minutes == 0
    assert result.study_coverage_ratio == 1.0


def test_no_trigger_retains_window_measurements() -> None:
    result = compute_reaction_features(_event(lambda minute: 100.5))

    assert result.classification == PathClassification.NO_MEANINGFUL
    assert result.reaction_timestamp is None
    assert result.reason is not None and result.reason.startswith("no_trigger_in_window")
    assert result.returns_pct[105] == pytest.approx(0.5)
    assert result.window_high_return_pct == pytest.approx(0.6)
    assert result.window_low_return_pct == pytest.approx(0.4)
    assert result.cumulative_volume[105] == 1050


@pytest.mark.parametrize("missing_minute", [0, 4, 14, 29, 59, 104])
def test_missing_exact_checkpoint_is_insufficient(missing_minute: int) -> None:
    result = compute_reaction_features(
        _event(lambda minute: 103.0, missing_minutes={missing_minute})
    )

    assert result.classification == PathClassification.INSUFFICIENT_DATA
    assert result.reason is not None and "missing exact checkpoint" in result.reason
    assert result.study_observed_minutes == 104
    assert result.study_missing_minutes == 1


def test_zero_volume_is_insufficient_and_vwap_is_disclosed() -> None:
    result = compute_reaction_features(_event(lambda minute: 103.0, volume=0))

    assert result.classification == PathClassification.INSUFFICIENT_DATA
    assert result.reaction_vwap_proxy is None
    assert result.volume_available is False
    assert result.reason is not None and "zero eligible volume" in result.reason


@pytest.mark.parametrize(
    ("close_at", "expected"),
    [
        (
            lambda minute: 100.0 if minute < 15 else 103.0,
            PathClassification.DELAYED,
        ),
        (
            lambda minute: 103.0 if minute < 5 else 100.5,
            PathClassification.FADE,
        ),
        (
            lambda minute: 103.0 if minute < 5 else 97.0,
            PathClassification.WHIPSAW,
        ),
    ],
)
def test_path_classification_precedence(
    close_at: Callable[[int], float], expected: PathClassification
) -> None:
    result = compute_reaction_features(_event(close_at))

    assert result.classification == expected


@pytest.mark.parametrize(
    ("trigger_minute", "delay", "expected"),
    [
        (13, 14, PathClassification.CONTINUATION),
        (14, 15, PathClassification.DELAYED),
    ],
)
def test_detection_delay_uses_close_availability_boundary(
    trigger_minute: int, delay: int, expected: PathClassification
) -> None:
    result = compute_reaction_features(
        _event(lambda minute: 100.0 if minute < trigger_minute else 103.0)
    )

    assert result.detection_delay_minutes == delay
    assert result.reaction_timestamp == START + dt.timedelta(minutes=delay)
    assert result.classification == expected


def test_delayed_collapse_is_classified_as_fade() -> None:
    def close_at(minute: int) -> float:
        if minute < 14:
            return 100.0
        if minute < 16:
            return 103.0
        return 100.5

    result = compute_reaction_features(_event(close_at))

    assert result.detection_delay_minutes == 15
    assert result.classification == PathClassification.FADE


def test_intrabar_spike_without_close_confirmation_does_not_trigger() -> None:
    event = _event(lambda minute: 100.5)
    bars = tuple(dataclasses.replace(bar, high=103.0) for bar in event.postmarket_bars)

    result = compute_reaction_features(dataclasses.replace(event, postmarket_bars=bars))

    assert result.classification == PathClassification.NO_MEANINGFUL
    assert result.reaction_timestamp is None


def test_post_signal_excursions_exclude_trigger_interval() -> None:
    event = _event(lambda minute: 103.0)
    bars = list(event.postmarket_bars)
    bars[0] = dataclasses.replace(bars[0], high=110.0)

    result = compute_reaction_features(
        dataclasses.replace(event, postmarket_bars=tuple(bars))
    )

    assert result.window_high_return_pct == pytest.approx(10.0)
    assert result.max_favorable_excursion_pct == pytest.approx(3.1)


@pytest.mark.parametrize("terminal", [100.5, 99.0])
def test_one_bar_trigger_followed_by_collapse_is_a_fade(terminal: float) -> None:
    result = compute_reaction_features(
        _event(lambda minute: 103.0 if minute == 0 else terminal)
    )

    assert result.classification == PathClassification.FADE
    assert result.max_favorable_excursion_pct is not None
    assert result.max_favorable_excursion_pct >= 3.0
    assert result.retracement_pct is not None
    assert result.retracement_pct > 50.0


@pytest.mark.parametrize(
    "event_change",
    [
        {"postmarket_stale": True},
        {"data_quality_flags": ("duplicate_minute_bars",)},
    ],
)
def test_stale_or_fatal_canonical_evidence_is_insufficient(event_change: dict) -> None:
    result = compute_reaction_features(
        dataclasses.replace(_event(lambda minute: 103.0), **event_change)
    )

    assert result.classification == PathClassification.INSUFFICIENT_DATA


def test_duplicate_timestamp_is_insufficient() -> None:
    event = _event(lambda minute: 103.0)
    duplicate = dataclasses.replace(event.postmarket_bars[0])
    result = compute_reaction_features(
        dataclasses.replace(
            event,
            postmarket_bars=event.postmarket_bars + (duplicate,),
        )
    )

    assert result.classification == PathClassification.INSUFFICIENT_DATA
    assert result.reason is not None and "duplicate bar timestamp" in result.reason


@pytest.mark.parametrize(
    "bad_bar",
    [
        {"close": float("nan")},
        {"low": 104.0},
        {"volume": -1},
    ],
)
def test_invalid_bar_values_are_insufficient(bad_bar: dict) -> None:
    event = _event(lambda minute: 103.0)
    bars = list(event.postmarket_bars)
    bars[10] = dataclasses.replace(bars[10], **bad_bar)

    result = compute_reaction_features(
        dataclasses.replace(event, postmarket_bars=tuple(bars))
    )

    assert result.classification == PathClassification.INSUFFICIENT_DATA


async def test_fetch_uses_only_the_coverage_selected_gateway_responses() -> None:
    regular_received = START + dt.timedelta(hours=4, minutes=5)
    post_received = START + dt.timedelta(hours=4, minutes=6)

    class CanonicalConnection:
        def __init__(self) -> None:
            self.phase_queries: list[tuple[str, tuple]] = []

        async def fetch(self, sql: str, *args):
            if "FROM earnings_events" in sql:
                return [
                    {
                        "symbol": "TEST",
                        "earnings_date": DATE,
                        "regular_expected_start": START - dt.timedelta(hours=6, minutes=30),
                        "regular_expected_end": START,
                        "post_expected_start": START,
                        "post_expected_end": START + dt.timedelta(hours=4),
                        "regular_received_at": regular_received,
                        "post_received_at": post_received,
                        "regular_source": "schwab",
                        "post_source": "schwab",
                        "regular_market_date": DATE,
                        "post_market_date": DATE,
                        "regular_collection_mode": "scheduled_capture",
                        "post_collection_mode": "scheduled_capture",
                        "regular_response_sha256": "a" * 64,
                        "post_response_sha256": "b" * 64,
                        "quality_flags": [],
                    }
                ]
            self.phase_queries.append((sql, args))
            assert "gateway_endpoint='/v1/session-history'" in sql
            assert "gateway_received_at=$3" in sql
            assert "ts >= $5 AND ts < $6" in sql
            received = args[2]
            if "session='regular'" in sql:
                assert received == regular_received
                timestamp = START - dt.timedelta(minutes=1)
            else:
                assert "session='extended'" in sql
                assert received == post_received
                timestamp = START
            return [
                {
                    "ts": timestamp,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10,
                    "stale": False,
                }
            ]

    conn = CanonicalConnection()
    [event] = await fetch_event_evidence(
        conn, from_date=DATE, to_date=DATE, symbols=("TEST",)
    )

    assert len(conn.phase_queries) == 2
    assert event.regular_response_sha256 == "a" * 64
    assert event.postmarket_response_sha256 == "b" * 64
    assert [bar.ts for bar in event.regular_bars] == [START - dt.timedelta(minutes=1)]
    assert [bar.ts for bar in event.postmarket_bars] == [START]

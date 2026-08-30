"""Plotly figure specifications, built server-side from research datasets.

Figures are plain dictionaries matching Plotly's JSON schema, so the browser only ever
receives data the Python layer computed.  No reaction threshold, retention ratio, or
classification is recomputed in JavaScript — the page draws exactly what the research
layer already decided.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from afterhours_lab.reactions import HORIZONS_MINUTES, REACTION_THRESHOLD_PCT
from afterhours_lab.research import EventDetail, EventSummary, PathBar

EASTERN = ZoneInfo("America/New_York")

REGULAR_TAIL_MINUTES = 30

UP_COLOR = "#1f9d76"
DOWN_COLOR = "#d1495b"
REFERENCE_COLOR = "#334155"
BAND_COLOR = "#94a3b8"
SIGNAL_COLOR = "#7c3aed"
VWAP_COLOR = "#0284c7"
MFE_COLOR = "#0f766e"
MAE_COLOR = "#b45309"
GRID_COLOR = "#e2e8f0"

BASE_LAYOUT: dict[str, Any] = {
    "template": "none",
    "paper_bgcolor": "#ffffff",
    "plot_bgcolor": "#ffffff",
    "font": {"family": "ui-monospace, SFMono-Regular, Menlo, monospace", "size": 11},
    "margin": {"l": 56, "r": 16, "t": 36, "b": 40},
    "hovermode": "x unified",
}

CHART_CONFIG: dict[str, Any] = {
    "displayModeBar": True,
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"],
}


def _et(value: dt.datetime) -> str:
    """Eastern wall-clock, tz-stripped: the axis is labelled ET, so an offset in the
    string would make Plotly re-render it in the viewer's own zone."""
    return value.astimezone(EASTERN).replace(tzinfo=None).isoformat()


def _hline(y: float, color: str, dash: str, text: str, width: float = 1.0) -> dict[str, Any]:
    return {
        "type": "line",
        "xref": "paper",
        "x0": 0,
        "x1": 1,
        "yref": "y",
        "y0": y,
        "y1": y,
        "line": {"color": color, "width": width, "dash": dash},
        "label": {"text": text, "textposition": "start", "font": {"size": 10, "color": color}},
    }


def _vline(x: str, color: str, dash: str, text: str) -> dict[str, Any]:
    return {
        "type": "line",
        "xref": "x",
        "x0": x,
        "x1": x,
        "yref": "paper",
        "y0": 0,
        "y1": 1,
        "line": {"color": color, "width": 1.2, "dash": dash},
        "label": {
            "text": text,
            "textposition": "top center",
            "font": {"size": 10, "color": color},
        },
    }


def _candles(bars: Sequence[PathBar], name: str, opacity: float = 1.0) -> dict[str, Any]:
    return {
        "type": "candlestick",
        "name": name,
        "x": [_et(bar.ts) for bar in bars],
        "open": [bar.open for bar in bars],
        "high": [bar.high for bar in bars],
        "low": [bar.low for bar in bars],
        "close": [bar.close for bar in bars],
        "increasing": {"line": {"color": UP_COLOR}, "fillcolor": UP_COLOR},
        "decreasing": {"line": {"color": DOWN_COLOR}, "fillcolor": DOWN_COLOR},
        "opacity": opacity,
        "xaxis": "x",
        "yaxis": "y",
    }


def _volume(bars: Sequence[PathBar], name: str = "Volume") -> dict[str, Any]:
    return {
        "type": "bar",
        "name": name,
        "x": [_et(bar.ts) for bar in bars],
        "y": [bar.volume or 0 for bar in bars],
        "marker": {
            "color": [
                UP_COLOR if bar.close >= bar.open else DOWN_COLOR for bar in bars
            ],
            "opacity": 0.55,
        },
        "xaxis": "x",
        "yaxis": "y2",
        "showlegend": False,
        "hovertemplate": "%{y:,} shares<extra></extra>",
    }


def _price_volume_layout(title: str) -> dict[str, Any]:
    """Price on the top 72% of the canvas, volume below, sharing one time axis."""
    return {
        **BASE_LAYOUT,
        "title": {"text": title, "x": 0, "font": {"size": 13}},
        "showlegend": True,
        "legend": {"orientation": "h", "y": 1.06, "x": 0},
        "xaxis": {
            "type": "date",
            "rangeslider": {"visible": False},
            "gridcolor": GRID_COLOR,
            "title": {"text": "Eastern time"},
        },
        "yaxis": {
            "domain": [0.28, 1.0],
            "gridcolor": GRID_COLOR,
            "title": {"text": "Price"},
        },
        "yaxis2": {
            "domain": [0.0, 0.2],
            "gridcolor": GRID_COLOR,
            "title": {"text": "Vol"},
        },
    }


def event_figure(detail: EventDetail) -> dict[str, Any]:
    """The earnings-day chart: regular tail, postmarket path, and every marker that
    explains how the stored classification was reached."""
    summary = detail.summary
    regular = detail.bars_for("earnings_regular")
    postmarket = detail.bars_for("earnings_postmarket")
    cutoff = detail.study_cutoff

    if regular:
        tail_start = regular[-1].ts - dt.timedelta(minutes=REGULAR_TAIL_MINUTES - 1)
        regular = tuple(bar for bar in regular if bar.ts >= tail_start)

    data: list[dict[str, Any]] = []
    if regular:
        data.append(_candles(regular, f"Regular (last {REGULAR_TAIL_MINUTES}m)"))
    if postmarket:
        in_window = tuple(bar for bar in postmarket if cutoff is None or bar.ts < cutoff)
        after_window = tuple(bar for bar in postmarket if cutoff is not None and bar.ts >= cutoff)
        if in_window:
            data.append(_candles(in_window, "Postmarket (study window)"))
        if after_window:
            data.append(_candles(after_window, "Postmarket (after 17:45)", opacity=0.35))
    volume_bars = tuple(regular) + tuple(postmarket)
    if volume_bars:
        data.append(_volume(volume_bars))

    shapes: list[dict[str, Any]] = []
    reference = summary.reference_price
    if reference:
        upper = reference * (1 + REACTION_THRESHOLD_PCT / 100.0)
        lower = reference * (1 - REACTION_THRESHOLD_PCT / 100.0)
        shapes.append(
            {
                "type": "rect",
                "xref": "paper",
                "x0": 0,
                "x1": 1,
                "yref": "y",
                "y0": lower,
                "y1": upper,
                "fillcolor": BAND_COLOR,
                "opacity": 0.10,
                "line": {"width": 0},
                "layer": "below",
            }
        )
        shapes.append(_hline(reference, REFERENCE_COLOR, "solid", "pre-close", width=1.4))
        shapes.append(
            _hline(upper, BAND_COLOR, "dot", f"+{REACTION_THRESHOLD_PCT:.0f}%")
        )
        shapes.append(
            _hline(lower, BAND_COLOR, "dot", f"-{REACTION_THRESHOLD_PCT:.0f}%")
        )
        if summary.reaction_vwap_proxy:
            shapes.append(
                _hline(summary.reaction_vwap_proxy, VWAP_COLOR, "dashdot", "VWAP proxy")
            )
        direction = 1.0 if summary.reaction_direction == "up" else -1.0
        if summary.max_favorable_excursion is not None:
            level = reference * (1 + direction * summary.max_favorable_excursion / 100.0)
            shapes.append(_hline(level, MFE_COLOR, "dash", "MFE"))
        if summary.max_adverse_excursion is not None:
            level = reference * (1 + direction * summary.max_adverse_excursion / 100.0)
            shapes.append(_hline(level, MAE_COLOR, "dash", "MAE"))

    if summary.reaction_timestamp:
        shapes.append(
            _vline(_et(summary.reaction_timestamp), SIGNAL_COLOR, "solid", "signal available")
        )
    if cutoff:
        shapes.append(_vline(_et(cutoff), REFERENCE_COLOR, "dot", "17:45 cutoff"))

    checkpoints = _checkpoint_trace(detail)
    if checkpoints:
        data.append(checkpoints)

    layout = _price_volume_layout(
        f"{summary.symbol} — {summary.earnings_date.isoformat()} after-close reaction"
    )
    layout["shapes"] = shapes
    return {"data": data, "layout": layout, "config": CHART_CONFIG}


def _checkpoint_trace(detail: EventDetail) -> dict[str, Any] | None:
    """PM+1/5/15/30/60/105 markers placed on the exact bars the features used."""
    start = detail.postmarket_start
    if start is None:
        return None
    by_ts = {bar.ts: bar for bar in detail.bars_for("earnings_postmarket")}
    xs: list[str] = []
    ys: list[float] = []
    texts: list[str] = []
    labels: list[str] = []
    summary = detail.summary
    returns = {
        1: summary.return_1m,
        5: summary.return_5m,
        15: summary.return_15m,
        30: summary.return_30m,
        60: summary.return_60m,
        105: summary.return_105m,
    }
    for minutes in HORIZONS_MINUTES:
        # Evidence timestamps identify interval starts, so the PM+n close is the bar
        # beginning at start + (n - 1) minutes.
        target = start + dt.timedelta(minutes=minutes - 1)
        bar = by_ts.get(target)
        if bar is None:
            continue
        xs.append(_et(target))
        ys.append(bar.close)
        texts.append(f"+{minutes}m")
        change = returns.get(minutes)
        labels.append(f"PM+{minutes}m {change:+.2f}%" if change is not None else f"PM+{minutes}m")
    if not xs:
        return None
    return {
        "type": "scatter",
        "mode": "markers+text",
        "name": "Checkpoints",
        "x": xs,
        "y": ys,
        "text": texts,
        "textposition": "top center",
        "textfont": {"size": 9, "color": REFERENCE_COLOR},
        "marker": {"size": 7, "color": SIGNAL_COLOR, "symbol": "diamond"},
        "customdata": labels,
        "hovertemplate": "%{customdata}<extra></extra>",
        "xaxis": "x",
        "yaxis": "y",
    }


def following_day_figure(detail: EventDetail) -> dict[str, Any] | None:
    """The next session, when captured: premarket into the regular open and close."""
    premarket = detail.bars_for("following_premarket")
    regular = detail.bars_for("following_regular")
    if not premarket and not regular:
        return None

    data: list[dict[str, Any]] = []
    if premarket:
        data.append(_candles(premarket, "Following premarket"))
    if regular:
        data.append(_candles(regular, "Following regular"))
    data.append(_volume(tuple(premarket) + tuple(regular)))

    shapes: list[dict[str, Any]] = []
    reference = detail.summary.reference_price
    if reference:
        shapes.append(
            _hline(reference, REFERENCE_COLOR, "solid", "earnings-day pre-close", width=1.4)
        )
    if regular:
        shapes.append(_vline(_et(regular[0].ts), SIGNAL_COLOR, "dot", "open"))
        shapes.append(_vline(_et(regular[-1].ts), SIGNAL_COLOR, "dot", "close"))

    layout = _price_volume_layout("Following session")
    layout["shapes"] = shapes
    return {"data": data, "layout": layout, "config": CHART_CONFIG}


# ------------------------------------------------------------------ explorer charts


def _scatter_layout(title: str, x_title: str, y_title: str) -> dict[str, Any]:
    return {
        **BASE_LAYOUT,
        "title": {"text": title, "x": 0, "font": {"size": 12}},
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.25, "x": 0, "font": {"size": 9}},
        "xaxis": {"title": {"text": x_title}, "gridcolor": GRID_COLOR, "zerolinecolor": "#cbd5e1"},
        "yaxis": {"title": {"text": y_title}, "gridcolor": GRID_COLOR, "zerolinecolor": "#cbd5e1"},
        "height": 320,
    }


CLASS_COLORS = {
    "immediate_continuation": "#1f9d76",
    "spike_and_fade": "#d1495b",
    "delayed_breakout": "#7c3aed",
    "whipsaw": "#b45309",
    "no_trigger_in_window": "#94a3b8",
    "not_analyzed": "#cbd5e1",
}


def _grouped_scatter(
    rows: Sequence[EventSummary],
    x_of,
    y_of,
    *,
    title: str,
    x_title: str,
    y_title: str,
) -> dict[str, Any]:
    """One trace per reaction class so the legend doubles as a class filter."""
    grouped: dict[str, list[EventSummary]] = {}
    for row in rows:
        x, y = x_of(row), y_of(row)
        if x is None or y is None:
            continue
        grouped.setdefault(row.reaction_class or "not_analyzed", []).append(row)

    data = [
        {
            "type": "scatter",
            "mode": "markers",
            "name": reaction_class,
            "x": [x_of(row) for row in group],
            "y": [y_of(row) for row in group],
            "text": [f"{row.symbol} {row.earnings_date.isoformat()}" for row in group],
            "marker": {
                "size": 7,
                "color": CLASS_COLORS.get(reaction_class, "#64748b"),
                "opacity": 0.8,
            },
            "hovertemplate": "%{text}<br>%{x:.2f} / %{y:.2f}<extra></extra>",
        }
        for reaction_class, group in sorted(grouped.items())
    ]
    return {
        "data": data,
        "layout": _scatter_layout(title, x_title, y_title),
        "config": CHART_CONFIG,
    }


def initial_vs_final_figure(rows: Sequence[EventSummary]) -> dict[str, Any]:
    return _grouped_scatter(
        rows,
        lambda row: row.initial_return,
        lambda row: row.return_105m,
        title="Initial reaction vs 105-minute return",
        x_title="Initial return %",
        y_title="PM+105m return %",
    )


def delay_vs_retention_figure(rows: Sequence[EventSummary]) -> dict[str, Any]:
    return _grouped_scatter(
        rows,
        lambda row: row.detection_delay_minutes,
        lambda row: row.retention,
        title="Detection delay vs retention",
        x_title="Detection delay (minutes after 16:00)",
        y_title="Retention (PM+105m / initial)",
    )


def surprise_vs_reaction_figure(rows: Sequence[EventSummary]) -> dict[str, Any]:
    return _grouped_scatter(
        rows,
        lambda row: row.eps_surprise_pct,
        lambda row: row.initial_return,
        title="EPS surprise vs price reaction",
        x_title="EPS surprise %",
        y_title="Initial return %",
    )


def class_distribution_figure(distribution: Sequence[tuple[str, int]]) -> dict[str, Any]:
    labels = [name for name, _ in distribution]
    return {
        "data": [
            {
                "type": "bar",
                "x": labels,
                "y": [count for _, count in distribution],
                "marker": {"color": [CLASS_COLORS.get(name, "#64748b") for name in labels]},
                "hovertemplate": "%{x}: %{y}<extra></extra>",
            }
        ],
        "layout": {
            **_scatter_layout("Distribution by reaction class", "", "Events"),
            "showlegend": False,
        },
        "config": CHART_CONFIG,
    }


def monthly_coverage_figure(months: Sequence[tuple[str, int, int]]) -> dict[str, Any]:
    labels = [month for month, _, _ in months]
    return {
        "data": [
            {
                "type": "bar",
                "name": "complete",
                "x": labels,
                "y": [analyzed for _, analyzed, _ in months],
                "marker": {"color": UP_COLOR},
            },
            {
                "type": "bar",
                "name": "not complete",
                "x": labels,
                "y": [total - analyzed for _, analyzed, total in months],
                "marker": {"color": "#cbd5e1"},
            },
        ],
        "layout": {
            **_scatter_layout("Events by month", "", "Events"),
            "barmode": "stack",
        },
        "config": CHART_CONFIG,
    }

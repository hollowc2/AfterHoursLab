import datetime as dt

import pytest

from afterhours_lab import moves

TODAY = dt.date(2026, 8, 19)


def candle(symbol, window, ts, *, open, close):
    return {
        "symbol": symbol,
        "capture_window": window,
        "earnings_date": TODAY,
        "ts": ts,
        "open": open,
        "close": close,
    }


class FakeConnection:
    """Mimics the LATERAL-join query in moves.fetch_moves against in-memory
    earnings_events/candles rows, so the join semantics (last day_before close,
    first/last day_after open/close, optional after_hours close) are exercised the
    same way the real SQL would, without a live Postgres."""

    def __init__(self, events: list[dict], candles: list[dict]) -> None:
        self._events = events
        self._candles = candles

    def _pick(self, symbol, earnings_date, window, field, *, latest: bool):
        matching = [
            c
            for c in self._candles
            if c["symbol"] == symbol
            and c["earnings_date"] == earnings_date
            and c["capture_window"] == window
        ]
        if not matching:
            return None
        matching.sort(key=lambda c: c["ts"], reverse=latest)
        return matching[0][field]

    async def fetch(self, sql: str, from_date, to_date):
        assert "earnings_events" in sql
        rows = []
        for e in self._events:
            if not (e["day_before_captured"] and e["day_after_captured"]):
                continue
            if from_date is not None and e["earnings_date"] < from_date:
                continue
            if to_date is not None and e["earnings_date"] > to_date:
                continue
            symbol, date = e["symbol"], e["earnings_date"]
            rows.append(
                {
                    "symbol": symbol,
                    "earnings_date": date,
                    "eps_estimate": e.get("eps_estimate"),
                    "eps_actual": e.get("eps_actual"),
                    "pre_close": self._pick(symbol, date, "day_before", "close", latest=True),
                    "ah_close": self._pick(symbol, date, "after_hours", "close", latest=True),
                    "post_open": self._pick(symbol, date, "day_after", "open", latest=False),
                    "post_close": self._pick(symbol, date, "day_after", "close", latest=True),
                }
            )
        rows.sort(key=lambda r: (r["earnings_date"], r["symbol"]), reverse=True)
        return rows


def event(symbol, **overrides):
    record = {
        "symbol": symbol,
        "earnings_date": TODAY,
        "day_before_captured": True,
        "day_after_captured": True,
        "eps_estimate": None,
        "eps_actual": None,
    }
    record.update(overrides)
    return record


async def test_full_window_computes_all_moves():
    events = [event("AAA", eps_estimate=1.00, eps_actual=1.10)]
    candles = [
        candle("AAA", "day_before", dt.datetime(2026, 8, 18, 20, 0), open=100.0, close=100.0),
        candle("AAA", "after_hours", dt.datetime(2026, 8, 19, 20, 5), open=108.0, close=110.0),
        candle("AAA", "day_after", dt.datetime(2026, 8, 20, 13, 30), open=111.0, close=115.0),
        candle("AAA", "day_after", dt.datetime(2026, 8, 20, 20, 0), open=114.0, close=112.0),
    ]
    conn = FakeConnection(events, candles)

    [result] = await moves.fetch_moves(conn)

    assert result.symbol == "AAA"
    assert result.pre_close == 100.0
    assert result.ah_close == 110.0
    assert result.post_open == 111.0
    assert result.post_close == 112.0
    assert result.reaction_move_pct == 10.0
    assert result.gap_move_pct == 11.0
    assert result.total_move_pct == 12.0
    assert result.eps_surprise_pct == pytest.approx(10.0)


async def test_missing_after_hours_window_leaves_reaction_none():
    events = [event("BBB")]
    candles = [
        candle("BBB", "day_before", dt.datetime(2026, 8, 18, 20, 0), open=50.0, close=50.0),
        candle("BBB", "day_after", dt.datetime(2026, 8, 20, 13, 30), open=45.0, close=48.0),
    ]
    conn = FakeConnection(events, candles)

    [result] = await moves.fetch_moves(conn)

    assert result.ah_close is None
    assert result.reaction_move_pct is None
    assert result.gap_move_pct == -10.0
    assert result.total_move_pct == -4.0


async def test_captured_flag_true_but_no_candle_rows_yields_none_moves():
    events = [event("CCC")]
    conn = FakeConnection(events, candles=[])

    [result] = await moves.fetch_moves(conn)

    assert result.pre_close is None
    assert result.reaction_move_pct is None
    assert result.gap_move_pct is None
    assert result.total_move_pct is None


async def test_incomplete_capture_excluded():
    events = [event("DDD", day_after_captured=False)]
    conn = FakeConnection(events, candles=[])

    results = await moves.fetch_moves(conn)

    assert results == []


async def test_date_range_filter():
    events = [
        event("EEE", earnings_date=dt.date(2026, 8, 10)),
        event("FFF", earnings_date=dt.date(2026, 8, 19)),
    ]
    conn = FakeConnection(events, candles=[])

    results = await moves.fetch_moves(
        conn, from_date=dt.date(2026, 8, 15), to_date=dt.date(2026, 8, 20)
    )

    assert [r.symbol for r in results] == ["FFF"]


def test_pct_change_guards_zero_and_none():
    assert moves._pct_change(None, 10.0) is None
    assert moves._pct_change(10.0, None) is None
    assert moves._pct_change(0.0, 10.0) is None
    assert moves._pct_change(50.0, 55.0) == 10.0

import datetime as dt

from schwab_gateway_sdk.models import SessionHistoryResponseV1

from afterhours_lab import capture_ohlcv

UTC = dt.timezone.utc


def response(symbol: str, date: dt.date, session: str) -> SessionHistoryResponseV1:
    # 08:00, 10:00, 17:00 ET on an EDT date.
    bars = [
        {
            "timestamp": dt.datetime(2026, 8, 26, hour, tzinfo=UTC),
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "volume": 10,
        }
        for hour in (12, 14, 21)
    ]
    return SessionHistoryResponseV1.model_validate(
        {
            "schema_version": "1.0",
            "session_history": {
                "symbol": symbol,
                "date": date,
                "session": session,
                "candles": bars,
                "event_timestamp": bars[-1]["timestamp"],
                "gateway_received_at": dt.datetime(2026, 8, 27, 1, tzinfo=UTC),
                "source": "schwab",
                "stale": False,
                "age_seconds": 0,
                "data_quality_flags": [],
            },
        }
    )


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    async def fetch(self, _sql, *_args):
        return [{"symbol": "AAPL"}]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class FakeGateway:
    def __init__(self, item) -> None:
        self.item = item
        self.calls: list[tuple] = []

    async def get_session_history(self, symbol, date, *, session):
        self.calls.append((symbol, date, session))
        return self.item


def test_phase_dates_use_earnings_day_and_following_trading_day() -> None:
    market_date = dt.date(2026, 8, 31)  # Monday
    assert capture_ohlcv.phase_dates(capture_ohlcv.PHASES["earnings_regular"], market_date) == (
        market_date,
        market_date,
    )
    assert capture_ohlcv.phase_dates(capture_ohlcv.PHASES["following_regular"], market_date) == (
        dt.date(2026, 8, 28),
        market_date,
    )


def test_phase_bounds_follow_scheduled_early_close() -> None:
    early_close = dt.date(2026, 11, 27)
    assert capture_ohlcv.phase_bounds(capture_ohlcv.PHASES["earnings_regular"], early_close) == (
        dt.datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        dt.datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
    )
    assert capture_ohlcv.phase_bounds(capture_ohlcv.PHASES["earnings_postmarket"], early_close) == (
        dt.datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        dt.datetime(2026, 11, 28, 1, 0, tzinfo=UTC),
    )


async def test_postmarket_coverage_filters_out_premarket_and_regular(monkeypatch) -> None:
    item = response("AAPL", dt.date(2026, 8, 26), "extended")
    conn = FakeConnection()
    gateway = FakeGateway(item)
    preserved: list[object] = []

    async def fake_preserve(_conn, value):
        preserved.append(value)

    monkeypatch.setattr(capture_ohlcv, "preserve_session_history", fake_preserve)
    count = await capture_ohlcv.capture_phase(
        gateway, conn, "earnings_postmarket", dt.date(2026, 8, 26)
    )

    assert count == 1
    assert gateway.calls == [("AAPL", dt.date(2026, 8, 26), "extended")]
    assert preserved == [item]  # raw response is retained unchanged
    args = conn.executed[0][1]
    assert args[2] == "earnings_postmarket"
    assert args[9] == 1
    assert args[10] == 240
    assert args[7] == dt.datetime(2026, 8, 26, 21, tzinfo=UTC)


async def test_premarket_phase_links_market_date_to_prior_earnings_date(monkeypatch) -> None:
    item = response("AAPL", dt.date(2026, 8, 26), "extended")
    conn = FakeConnection()
    gateway = FakeGateway(item)

    async def fake_preserve(_conn, _value):
        return None

    monkeypatch.setattr(capture_ohlcv, "preserve_session_history", fake_preserve)
    await capture_ohlcv.capture_phase(gateway, conn, "following_premarket", dt.date(2026, 8, 26))

    args = conn.executed[0][1]
    assert args[1] == dt.date(2026, 8, 25)
    assert args[3] == dt.date(2026, 8, 26)
    assert args[9] == 1
    assert args[10] == 330


async def test_main_skips_exchange_holiday_before_connecting(capsys) -> None:
    result = await capture_ohlcv._main(
        ["--phase", "earnings_regular", "--market-date", "2026-11-26"]
    )
    assert result == 0
    assert "not an XNYS session; skipped" in capsys.readouterr().out

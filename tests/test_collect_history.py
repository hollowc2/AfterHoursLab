import datetime as dt

import pytest

from afterhours_lab.collect_history import collect, parse_args


class FakeGateway:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple] = []

    async def get_session_history(self, symbol, date, *, session):
        self.calls.append((symbol, date, session))
        return self.response

    async def get_history(self, symbol, *, frequency, days_back):
        self.calls.append((symbol, frequency, days_back))
        return self.response


async def test_collect_extended_session_history(monkeypatch) -> None:
    response = object()
    gateway = FakeGateway(response)
    preserved: list[object] = []

    async def fake_preserve(_conn, item) -> None:
        preserved.append(item)

    monkeypatch.setattr(
        "afterhours_lab.collect_history.preserve_session_history", fake_preserve
    )
    count = await collect(
        gateway,
        object(),
        ["AAPL", "MSFT"],
        date=dt.date(2026, 8, 25),
        days_back=None,
    )

    assert count == 2
    assert gateway.calls == [
        ("AAPL", dt.date(2026, 8, 25), "extended"),
        ("MSFT", dt.date(2026, 8, 25), "extended"),
    ]
    assert preserved == [response, response]


async def test_collect_trailing_minute_history(monkeypatch) -> None:
    response = object()
    gateway = FakeGateway(response)
    preserved: list[object] = []

    async def fake_preserve(_conn, item) -> None:
        preserved.append(item)

    monkeypatch.setattr(
        "afterhours_lab.collect_history.preserve_minute_history", fake_preserve
    )
    count = await collect(
        gateway, object(), ["AAPL"], date=None, days_back=2
    )

    assert count == 1
    assert gateway.calls == [("AAPL", "minute", 2)]
    assert preserved == [response]


def test_parse_args_normalizes_and_deduplicates_symbols() -> None:
    args = parse_args(["aapl", "AAPL", "msft", "--date", "2026-08-25"])
    assert args.symbols == ["AAPL", "MSFT"]
    assert args.date == dt.date(2026, 8, 25)


@pytest.mark.parametrize("days_back", ["0", "21"])
def test_parse_args_bounds_history_window(days_back: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["AAPL", "--days-back", days_back])

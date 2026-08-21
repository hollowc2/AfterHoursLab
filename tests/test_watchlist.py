import datetime as dt
import json
import threading
import time

import pytest

from afterhours_lab import watchlist
from afterhours_lab.watchlist import (
    Watchlist,
    add_symbol,
    load_watchlist,
    read_watchlist,
    remove_symbol,
    save_watchlist,
)


def test_load_watchlist_missing_file_returns_empty(tmp_path) -> None:
    assert load_watchlist(tmp_path / "watchlist.json") == []


def test_save_then_load_round_trips(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY", "QQQ"], path)
    assert load_watchlist(path) == ["SPY", "QQQ"]


def test_load_watchlist_rejects_non_list(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        load_watchlist(path)


def test_add_symbol_is_idempotent(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    add_symbol("SPY", path)
    symbols = add_symbol("SPY", path)
    assert symbols == ["SPY"]


def test_remove_symbol(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY", "QQQ"], path)
    symbols = remove_symbol("SPY", path)
    assert symbols == ["QQQ"]


def test_concurrent_add_symbol_calls_do_not_lose_either_write(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two add_symbol calls racing each other must serialize on the file lock rather
    than interleaving their load-mutate-save, which would otherwise silently drop
    whichever write finished first (classic lost-update race)."""
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY"], path)

    real_read = watchlist.read_watchlist
    first_call_started = threading.Event()
    release_first_call = threading.Event()
    call_count = {"n": 0}

    def gated_read(p):
        # add_symbol acquires the lock before ever calling read_watchlist, so a
        # second, concurrent add_symbol can only reach this function after the
        # first one has released the lock — making "the first call to arrive here"
        # an unambiguous way to gate exactly one of the two calls.
        result = real_read(p)
        call_count["n"] += 1
        if call_count["n"] == 1:
            first_call_started.set()
            assert release_first_call.wait(timeout=5), "second call never blocked on the lock"
        return result

    monkeypatch.setattr(watchlist, "read_watchlist", gated_read)

    results: dict[str, list[str]] = {}

    def add(symbol: str) -> None:
        results[symbol] = watchlist.add_symbol(symbol, path)

    first = threading.Thread(target=add, args=("AAA",))
    first.start()
    assert first_call_started.wait(timeout=5)

    second = threading.Thread(target=add, args=("BBB",))
    second.start()

    # Neither thread should be able to finish yet: `first` is gated on
    # release_first_call, and `second` should be blocked behind the file lock
    # `first` is still holding (not free to interleave and write concurrently).
    time.sleep(0.1)
    assert results == {}

    release_first_call.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert set(load_watchlist(path)) == {"SPY", "AAA", "BBB"}


def test_add_symbol_waits_for_a_concurrent_save_watchlist(tmp_path) -> None:
    """add_symbol must not read a stale snapshot while save_watchlist is mid-write —
    it should block on the lock until save_watchlist finishes, not lose its own write
    to it."""
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY"], path)

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock_then_write() -> None:
        with watchlist._locked(path):
            lock_acquired.set()
            assert release_lock.wait(timeout=5), "add_symbol never attempted to acquire the lock"
            path.write_text('["SPY", "HELD"]\n')

    holder = threading.Thread(target=hold_lock_then_write)
    holder.start()
    assert lock_acquired.wait(timeout=5)

    results: dict[str, list[str]] = {}

    def add() -> None:
        results["AAA"] = watchlist.add_symbol("AAA", path)

    waiter = threading.Thread(target=add)
    waiter.start()

    time.sleep(0.1)
    assert results == {}  # still blocked behind the held lock

    release_lock.set()
    holder.join(timeout=5)
    waiter.join(timeout=5)

    assert set(results["AAA"]) == {"SPY", "HELD", "AAA"}


def test_save_stamps_as_of_and_writes_the_object_form(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY"], path, as_of=dt.date(2026, 8, 21))
    assert json.loads(path.read_text()) == {"as_of": "2026-08-21", "symbols": ["SPY"]}


def test_save_defaults_as_of_to_today(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY"], path)
    assert read_watchlist(path).as_of == dt.date.today()


def test_legacy_bare_array_still_loads_but_has_no_as_of(tmp_path) -> None:
    """The pre-as_of on-disk format — and what a human hand-writes — must keep
    working rather than erroring out mid-session."""
    path = tmp_path / "watchlist.json"
    path.write_text('["SPY", "QQQ"]\n')
    result = read_watchlist(path)
    assert result.symbols == ["SPY", "QQQ"]
    assert result.as_of is None


def test_missing_file_is_empty_and_stale(tmp_path) -> None:
    result = read_watchlist(tmp_path / "watchlist.json")
    assert result.symbols == []
    assert result.is_stale(dt.date(2026, 8, 21))


def test_load_watchlist_rejects_object_without_symbols(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text('{"as_of": "2026-08-21"}')
    with pytest.raises(ValueError):
        load_watchlist(path)


def test_load_watchlist_rejects_unparseable_as_of(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text('{"as_of": "not-a-date", "symbols": []}')
    with pytest.raises(ValueError):
        load_watchlist(path)


def test_same_day_watchlist_is_not_stale() -> None:
    friday = dt.date(2026, 8, 21)
    assert not Watchlist(["SPY"], friday).is_stale(friday)


def test_friday_watchlist_read_over_the_weekend_is_not_stale() -> None:
    """No archive run is scheduled on a weekend, so Friday's file is still the
    current one on Saturday and Sunday — flagging it stale would be crying wolf."""
    friday = dt.date(2026, 8, 21)
    for weekend_day in (dt.date(2026, 8, 22), dt.date(2026, 8, 23)):
        assert not Watchlist(["SPY"], friday).is_stale(weekend_day)


def test_watchlist_from_a_previous_trading_day_is_stale() -> None:
    stale = Watchlist(["SPY"], dt.date(2026, 8, 19))
    assert stale.is_stale(dt.date(2026, 8, 21))
    message = stale.staleness_message(dt.date(2026, 8, 21))
    assert message is not None
    assert "2026-08-19" in message and "2026-08-21" in message


def test_legacy_watchlist_without_as_of_is_stale() -> None:
    result = Watchlist(["SPY"])
    assert result.is_stale(dt.date(2026, 8, 21))
    assert "no as_of" in (result.staleness_message(dt.date(2026, 8, 21)) or "")


def test_staleness_message_is_none_when_current() -> None:
    friday = dt.date(2026, 8, 21)
    assert Watchlist(["SPY"], friday).staleness_message(friday) is None


def test_add_and_remove_preserve_as_of(tmp_path) -> None:
    """A manual --add doesn't make a stale list current: only an archive run decides
    which symbols are in the capture window, so the recorded date must not move."""
    path = tmp_path / "watchlist.json"
    save_watchlist(["SPY"], path, as_of=dt.date(2026, 8, 19))
    add_symbol("AAA", path)
    assert read_watchlist(path).as_of == dt.date(2026, 8, 19)
    remove_symbol("SPY", path)
    assert read_watchlist(path).as_of == dt.date(2026, 8, 19)


def test_add_symbol_on_a_legacy_file_keeps_it_as_of_less(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text('["SPY"]\n')
    assert add_symbol("AAA", path) == ["SPY", "AAA"]
    assert read_watchlist(path).as_of is None

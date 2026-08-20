import threading
import time

import pytest

from afterhours_lab import watchlist
from afterhours_lab.watchlist import add_symbol, load_watchlist, remove_symbol, save_watchlist


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

    real_load = watchlist.load_watchlist
    first_call_started = threading.Event()
    release_first_call = threading.Event()
    call_count = {"n": 0}

    def gated_load(p):
        # add_symbol acquires the lock before ever calling load_watchlist, so a
        # second, concurrent add_symbol can only reach this function after the
        # first one has released the lock — making "the first call to arrive here"
        # an unambiguous way to gate exactly one of the two calls.
        result = real_load(p)
        call_count["n"] += 1
        if call_count["n"] == 1:
            first_call_started.set()
            assert release_first_call.wait(timeout=5), "second call never blocked on the lock"
        return result

    monkeypatch.setattr(watchlist, "load_watchlist", gated_load)

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

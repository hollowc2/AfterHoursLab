import pytest

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

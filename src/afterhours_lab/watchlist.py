"""Persisted watchlist storage. A flat JSON list of symbols on disk."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_WATCHLIST_PATH = Path("watchlist.json")


def load_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path} must contain a JSON array of symbol strings")
    return data


def save_watchlist(symbols: list[str], path: Path = DEFAULT_WATCHLIST_PATH) -> None:
    path.write_text(json.dumps(symbols, indent=2) + "\n")


def add_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    symbols = load_watchlist(path)
    if symbol not in symbols:
        symbols.append(symbol)
        save_watchlist(symbols, path)
    return symbols


def remove_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    symbols = [s for s in load_watchlist(path) if s != symbol]
    save_watchlist(symbols, path)
    return symbols

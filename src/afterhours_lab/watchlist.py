"""Persisted watchlist storage. A flat JSON list of symbols on disk.

Multiple processes can touch the same watchlist file — archive_earnings.py rewrites
it wholesale each run, watch.py's --add/--remove do a read-modify-write from an
operator's terminal — with no other coordination between them. Every mutating
operation here acquires an exclusive file lock (fcntl.flock on a sibling `.lock`
file) for its full duration, so two overlapping writes serialize instead of
interleaving and silently losing one of them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path
from typing import Iterator

DEFAULT_WATCHLIST_PATH = Path("watchlist.json")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Exclusive lock on `path`'s sibling `.lock` file, held for one full
    read-modify-write watchlist operation. The lock file is created if missing and
    is never removed — its content doesn't matter, only its existence as a lock
    target.

    fcntl.flock locks are per-open-file-description, so each call here opens its own
    fd rather than reusing a module-level one: two calls from the same process (e.g.
    a caller that mistakenly nested locked operations) would otherwise deadlock
    instead of serializing. Callers in this module avoid that by never calling one
    locked function from inside another.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def load_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path} must contain a JSON array of symbol strings")
    return data


def save_watchlist(symbols: list[str], path: Path = DEFAULT_WATCHLIST_PATH) -> None:
    # Locked even though this doesn't read first: without the lock, a save_watchlist
    # racing an add_symbol/remove_symbol's read-modify-write could still land its
    # write in the middle of the other's, clobbering whichever one finishes last.
    with _locked(path):
        path.write_text(json.dumps(symbols, indent=2) + "\n")


def add_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    with _locked(path):
        symbols = load_watchlist(path)
        if symbol not in symbols:
            symbols.append(symbol)
            path.write_text(json.dumps(symbols, indent=2) + "\n")
        return symbols


def remove_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    with _locked(path):
        symbols = [s for s in load_watchlist(path) if s != symbol]
        path.write_text(json.dumps(symbols, indent=2) + "\n")
        return symbols

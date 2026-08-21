"""Persisted watchlist storage: the current capture window's symbols, on disk.

This file is a *cache*, not a record. `archive_earnings.py` rewrites it wholesale
each run from an `earnings_events` query, so Postgres — not this file — is the
history of record, and any past day's watchlist can be reconstructed by re-running
that query for a different date. Nothing here tries to keep history.

What the file does carry is `as_of`: the date the archive run that wrote it was
covering. Without it a stale watchlist is indistinguishable from a current one, which
is exactly how a list of symbols from two days earlier can end up on screen looking
authoritative. `is_stale` turns that into something a caller can check.

On-disk shape:

    {"as_of": "2026-08-21", "symbols": ["AAA", "BBB"]}

A bare JSON array is still accepted on read (the pre-`as_of` format, and a
convenient thing to hand-write) and reports as having no `as_of` — which counts as
stale, since an unknown write date can't be shown to be current.

Multiple processes can touch the same watchlist file — archive_earnings.py rewrites
it wholesale each run, watch.py's --add/--remove do a read-modify-write from an
operator's terminal — with no other coordination between them. Every mutating
operation here acquires an exclusive file lock (fcntl.flock on a sibling `.lock`
file) for its full duration, so two overlapping writes serialize instead of
interleaving and silently losing one of them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
from pathlib import Path
from typing import Iterator

from afterhours_lab.trading_calendar import latest_trading_day

DEFAULT_WATCHLIST_PATH = Path("watchlist.json")


@dataclasses.dataclass(frozen=True)
class Watchlist:
    """The parsed watchlist file: its symbols plus the date they were current for.

    `as_of` is None only for a legacy bare-array file, which is treated as stale.
    """

    symbols: list[str]
    as_of: dt.date | None = None

    def is_stale(self, today: dt.date | None = None) -> bool:
        """True when this file predates the most recent trading day — i.e. an archive
        run should have refreshed it by now and evidently didn't.

        Compared against `latest_trading_day` rather than the raw date so a Friday
        watchlist read over the weekend is correctly current, not stale.
        """
        if self.as_of is None:
            return True
        today = today if today is not None else dt.date.today()
        return self.as_of < latest_trading_day(today)

    def staleness_message(self, today: dt.date | None = None) -> str | None:
        """A one-line human explanation when stale, else None."""
        if not self.is_stale(today):
            return None
        if self.as_of is None:
            return "watchlist has no as_of date (pre-dated format); it may be out of date"
        today = today if today is not None else dt.date.today()
        return (
            f"watchlist is stale: written for {self.as_of.isoformat()}, "
            f"but the current trading day is {latest_trading_day(today).isoformat()} "
            f"— run afterhours-lab-archive-earnings"
        )


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


def _parse(data: object, path: Path) -> Watchlist:
    if isinstance(data, list):
        # Legacy bare-array file: symbols with no recorded write date.
        if not all(isinstance(item, str) for item in data):
            raise ValueError(f"{path} must contain a JSON array of symbol strings")
        return Watchlist(symbols=data)

    if isinstance(data, dict):
        symbols = data.get("symbols")
        if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
            raise ValueError(f"{path} must have a 'symbols' array of symbol strings")
        raw_as_of = data.get("as_of")
        if raw_as_of is None:
            return Watchlist(symbols=symbols)
        if not isinstance(raw_as_of, str):
            raise ValueError(f"{path} has a non-string 'as_of'")
        try:
            as_of = dt.date.fromisoformat(raw_as_of)
        except ValueError as exc:
            raise ValueError(f"{path} has an unparseable 'as_of': {raw_as_of!r}") from exc
        return Watchlist(symbols=symbols, as_of=as_of)

    raise ValueError(f"{path} must contain a watchlist object or a JSON array of symbols")


def _serialize(watchlist: Watchlist) -> str:
    payload: dict[str, object] = {}
    if watchlist.as_of is not None:
        payload["as_of"] = watchlist.as_of.isoformat()
    payload["symbols"] = watchlist.symbols
    return json.dumps(payload, indent=2) + "\n"


def read_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> Watchlist:
    """The full watchlist document, including `as_of`. Use this when freshness
    matters; `load_watchlist` is the symbols-only shorthand."""
    if not path.exists():
        return Watchlist(symbols=[])
    return _parse(json.loads(path.read_text()), path)


def load_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    """Just the symbols, for callers that don't care when the file was written."""
    return read_watchlist(path).symbols


def save_watchlist(
    symbols: list[str],
    path: Path = DEFAULT_WATCHLIST_PATH,
    *,
    as_of: dt.date | None = None,
) -> None:
    """Replace the file with `symbols`, stamped `as_of` (today when not given).

    Locked even though this doesn't read first: without the lock, a save_watchlist
    racing an add_symbol/remove_symbol's read-modify-write could still land its
    write in the middle of the other's, clobbering whichever one finishes last.
    """
    as_of = as_of if as_of is not None else dt.date.today()
    with _locked(path):
        path.write_text(_serialize(Watchlist(symbols=symbols, as_of=as_of)))


def add_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    """Add one symbol, leaving `as_of` untouched.

    A manual add doesn't make a stale file current — the archive run is what decides
    which symbols are in the capture window — so the existing date is preserved
    rather than refreshed to today.
    """
    with _locked(path):
        watchlist = read_watchlist(path)
        if symbol in watchlist.symbols:
            return watchlist.symbols
        symbols = [*watchlist.symbols, symbol]
        path.write_text(_serialize(dataclasses.replace(watchlist, symbols=symbols)))
        return symbols


def remove_symbol(symbol: str, path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    """Remove one symbol, leaving `as_of` untouched (see `add_symbol`)."""
    with _locked(path):
        watchlist = read_watchlist(path)
        symbols = [s for s in watchlist.symbols if s != symbol]
        path.write_text(_serialize(dataclasses.replace(watchlist, symbols=symbols)))
        return symbols

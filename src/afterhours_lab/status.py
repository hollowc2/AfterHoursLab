"""Last-run status breadcrumb, shared by archive_earnings.py and capture.py.

archive_earnings.log on helios was, until now, the only record of a run's outcome —
nothing reads it unless someone goes looking. This writes a small JSON file next to
the watchlist on every run (success, failure, or skip) so a human or a future check
script can see the last outcome without grepping the log. capture.py passes a
different `filename` so the two scripts' status files don't clobber each other when
both point at the same watchlist directory.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DEFAULT_STATUS_FILENAME = "last_run_status.json"


def status_path_for(watchlist_path: Path, filename: str = DEFAULT_STATUS_FILENAME) -> Path:
    return watchlist_path.parent / filename


def write_status(path: Path, *, ok: bool, detail: str) -> None:
    payload = {
        "ok": ok,
        "detail": detail,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_status(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())

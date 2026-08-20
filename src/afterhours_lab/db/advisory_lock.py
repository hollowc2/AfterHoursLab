"""Shared Postgres advisory-lock context managers.

db/migrate.py, archive_earnings.py, and capture.py each held one connection under a
`pg_advisory_lock`/`pg_try_advisory_lock` for the duration of some work, released in a
`finally` that called `pg_advisory_unlock` on the same connection. That shape was
hand-rolled three times, and it had a bug: if the connection died mid-operation, the
`finally`'s own unlock call likely also raised — and because it raised inside a
`finally`, that new exception replaced whatever real exception was propagating out of
the `try` block, so the wrong error (an unlock failure, not the actual root cause)
ended up wherever the caller reports failures (last_run_status.json, Telegram, etc).

Both context managers here route their release through `_unlock`, which catches and
logs an unlock failure instead of raising it, so it can never mask an exception from
the caller's `async with` body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

log = structlog.get_logger()


async def _unlock(conn, key: int) -> None:
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", key)
    except Exception as exc:
        log.error("advisory_unlock_failed", key=key, error=str(exc))


@asynccontextmanager
async def advisory_lock(conn, key: int) -> AsyncIterator[None]:
    """Blocking pg_advisory_lock, held for the duration of the `async with` block.
    For db/migrate.py, where a concurrent migration run should wait rather than skip."""
    await conn.execute("SELECT pg_advisory_lock($1)", key)
    try:
        yield
    finally:
        await _unlock(conn, key)


@asynccontextmanager
async def try_advisory_lock(conn, key: int) -> AsyncIterator[bool]:
    """Non-blocking pg_try_advisory_lock. Yields whether the lock was acquired; the
    caller is responsible for skipping its work when it wasn't (`if not acquired:
    return None ...`). The lock is only released on exit if it was actually acquired.
    For archive_earnings.py and capture.py, where a stuck/overlapping run should make
    a fresh cron fire skip rather than queue behind a possibly wedged process."""
    acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
    try:
        yield acquired
    finally:
        if acquired:
            await _unlock(conn, key)

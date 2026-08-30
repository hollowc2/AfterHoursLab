"""Thin asyncpg pool wrapper for AfterHoursLab's own database."""

from __future__ import annotations

import asyncpg

from afterhours_lab.db.config import DatabaseSettings


async def _make_read_only(conn: asyncpg.Connection) -> None:
    await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")


class DatabasePool:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls, settings: DatabaseSettings, *, read_only: bool = False
    ) -> DatabasePool:
        """Open a pool. ``read_only=True`` fixes every connection's session to
        ``TRANSACTION READ ONLY`` so a caller that should only observe — a notebook,
        a report — cannot write even by mistake."""
        init = _make_read_only if read_only else None
        pool = await asyncpg.create_pool(dsn=settings.dsn, init=init)
        return cls(pool)

    def acquire(self):
        return self._pool.acquire()

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> DatabasePool:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

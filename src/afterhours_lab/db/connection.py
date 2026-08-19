"""Thin asyncpg pool wrapper for AfterHoursLab's own database."""

from __future__ import annotations

import asyncpg

from afterhours_lab.db.config import DatabaseSettings


class DatabasePool:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, settings: DatabaseSettings) -> DatabasePool:
        pool = await asyncpg.create_pool(dsn=settings.dsn)
        return cls(pool)

    def acquire(self):
        return self._pool.acquire()

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> DatabasePool:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

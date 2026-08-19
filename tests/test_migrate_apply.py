"""apply_migrations() exercised against a minimal in-memory fake of the asyncpg surface
it actually uses (acquire/execute/fetch/transaction), since no real Postgres is
available in this environment."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from afterhours_lab.db.migrate import Migration, apply_migrations


class FakeConnection:
    def __init__(self) -> None:
        self.schema_migrations: dict[str, str] = {}
        self.executed_sql: list[str] = []

    async def execute(self, sql: str, *args: object) -> None:
        self.executed_sql.append(sql)
        if sql.startswith("INSERT INTO schema_migrations"):
            version, checksum = args
            self.schema_migrations[version] = checksum

    async def fetch(self, sql: str) -> list[dict[str, str]]:
        return [
            {"version": version, "checksum": checksum}
            for version, checksum in self.schema_migrations.items()
        ]

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


def make_migration(version: str, sql: str = "SELECT 1;") -> Migration:
    return Migration(version=version, path=None, sql=sql)


async def test_apply_migrations_applies_all_when_none_applied() -> None:
    conn = FakeConnection()
    pool = FakePool(conn)
    migrations = [make_migration("001_a"), make_migration("002_b")]

    applied = await apply_migrations(pool, migrations)

    assert applied == ["001_a", "002_b"]
    assert set(conn.schema_migrations) == {"001_a", "002_b"}


async def test_apply_migrations_skips_already_applied() -> None:
    conn = FakeConnection()
    migration = make_migration("001_a")
    conn.schema_migrations[migration.version] = migration.checksum
    pool = FakePool(conn)

    applied = await apply_migrations(pool, [migration, make_migration("002_b")])

    assert applied == ["002_b"]


async def test_apply_migrations_raises_on_checksum_drift() -> None:
    conn = FakeConnection()
    migration = make_migration("001_a", sql="SELECT 1;")
    conn.schema_migrations[migration.version] = "stale-checksum"
    pool = FakePool(conn)

    with pytest.raises(ValueError, match="changed since it was applied"):
        await apply_migrations(pool, [migration])

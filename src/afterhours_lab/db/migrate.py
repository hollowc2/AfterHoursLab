"""Apply raw-SQL migrations in filename order, tracked in a schema_migrations ledger.

Mirrors Butterflyguy's migration convention: numbered SQL files, a ledger table of
applied version + checksum, and a Postgres advisory lock so concurrent runs can't race.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import structlog

from afterhours_lab.db.config import DatabaseSettings

log = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary fixed key for pg_advisory_lock; unique to this app so it can't collide with
# another app's migration lock on the same shared Postgres instance.
ADVISORY_LOCK_KEY = 0x41465445524C4142  # the 8 ASCII bytes of "AFTERLAB" as an int64


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Migrations in filename order. Each filename must start with a sortable version
    prefix, e.g. 001_initial.sql."""
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        migrations.append(Migration(version=version, path=path, sql=path.read_text()))
    return migrations


async def apply_migrations(
    pool: asyncpg.Pool, migrations: list[Migration] | None = None
) -> list[str]:
    """Apply any not-yet-applied migrations. Returns the versions newly applied."""
    migrations = migrations if migrations is not None else discover_migrations()
    applied: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            existing = {
                row["version"]: row["checksum"]
                for row in await conn.fetch("SELECT version, checksum FROM schema_migrations")
            }
            for migration in migrations:
                if migration.version in existing:
                    if existing[migration.version] != migration.checksum:
                        raise ValueError(
                            f"migration {migration.version} has changed since it was applied"
                        )
                    continue
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                        migration.version,
                        migration.checksum,
                    )
                log.info("migration_applied", version=migration.version)
                applied.append(migration.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)

    return applied


async def _main() -> int:
    settings = DatabaseSettings()
    pool = await asyncpg.create_pool(dsn=settings.dsn)
    try:
        applied = await apply_migrations(pool)
    finally:
        await pool.close()
    if applied:
        print(f"applied: {applied}")
    else:
        print("no pending migrations")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()

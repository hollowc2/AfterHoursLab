import pytest

from afterhours_lab.db import advisory_lock as advisory_lock_module
from afterhours_lab.db.advisory_lock import advisory_lock, try_advisory_lock


class FakeConnection:
    """Minimal asyncpg-surface fake: execute (lock/unlock, raising on unlock if
    configured) and fetchval (the try-lock's acquired/not-acquired result)."""

    def __init__(self, *, unlock_error: Exception | None = None, lock_available: bool = True):
        self._unlock_error = unlock_error
        self._lock_available = lock_available
        self.executed: list[str] = []

    async def execute(self, sql: str, *_args: object) -> None:
        self.executed.append(sql)
        if sql.startswith("SELECT pg_advisory_unlock") and self._unlock_error is not None:
            raise self._unlock_error

    async def fetchval(self, sql: str, *_args: object) -> bool:
        self.executed.append(sql)
        return self._lock_available


async def test_advisory_lock_acquires_and_releases() -> None:
    conn = FakeConnection()

    async with advisory_lock(conn, 1):
        pass

    assert conn.executed == ["SELECT pg_advisory_lock($1)", "SELECT pg_advisory_unlock($1)"]


async def test_advisory_lock_unlock_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConnection(unlock_error=RuntimeError("connection closed"))
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        advisory_lock_module.log, "error", lambda event, **kw: logged.append((event, kw))
    )

    async with advisory_lock(conn, 1):
        pass  # no exception should escape the unlock failure above

    assert logged
    assert logged[0][0] == "advisory_unlock_failed"


async def test_advisory_lock_unlock_failure_does_not_mask_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConnection(unlock_error=RuntimeError("connection closed"))
    monkeypatch.setattr(advisory_lock_module.log, "error", lambda *_a, **_kw: None)

    with pytest.raises(ValueError, match="real error"):
        async with advisory_lock(conn, 1):
            raise ValueError("real error")


async def test_try_advisory_lock_yields_true_and_unlocks_when_acquired() -> None:
    conn = FakeConnection(lock_available=True)

    async with try_advisory_lock(conn, 1) as acquired:
        assert acquired is True

    assert conn.executed == ["SELECT pg_try_advisory_lock($1)", "SELECT pg_advisory_unlock($1)"]


async def test_try_advisory_lock_yields_false_and_never_unlocks_when_unavailable() -> None:
    conn = FakeConnection(lock_available=False)

    async with try_advisory_lock(conn, 1) as acquired:
        assert acquired is False

    assert conn.executed == ["SELECT pg_try_advisory_lock($1)"]


async def test_try_advisory_lock_unlock_failure_does_not_mask_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConnection(lock_available=True, unlock_error=RuntimeError("connection closed"))
    monkeypatch.setattr(advisory_lock_module.log, "error", lambda *_a, **_kw: None)

    with pytest.raises(ValueError, match="real error"):
        async with try_advisory_lock(conn, 1) as acquired:
            assert acquired is True
            raise ValueError("real error")

from pathlib import Path

from afterhours_lab.status import load_status, status_path_for, write_status


def test_status_path_for_is_sibling_of_watchlist() -> None:
    assert status_path_for(Path("/app/data/watchlist.json")) == Path(
        "/app/data/last_run_status.json"
    )


def test_load_status_missing_file_returns_none(tmp_path) -> None:
    assert load_status(tmp_path / "last_run_status.json") is None


def test_write_then_load_round_trips(tmp_path) -> None:
    path = tmp_path / "last_run_status.json"
    write_status(path, ok=True, detail="archived 1 new event(s)")

    status = load_status(path)

    assert status["ok"] is True
    assert status["detail"] == "archived 1 new event(s)"
    assert "at" in status

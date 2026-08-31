from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from afterhours_lab import persist_outcomes, study_cli

SPEC_PATH = Path(__file__).parents[1] / "studies/delay-retention-example.json"


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.closed = False

    def acquire(self):
        return _Acquire(self.conn)

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def acquired_lock(*_args):
    yield True


@asynccontextmanager
async def unavailable_lock(*_args):
    yield False


@pytest.mark.asyncio
async def test_outcome_cli_fake_database_lifecycle_and_cleanup(monkeypatch, capsys) -> None:
    pool = FakePool(object())

    async def connect(_settings):
        return pool

    async def evidence(*_args, **_kwargs):
        return ()

    monkeypatch.setattr(persist_outcomes.DatabasePool, "connect", connect)
    monkeypatch.setattr(persist_outcomes, "DatabaseSettings", object)
    monkeypatch.setattr(persist_outcomes, "try_advisory_lock", acquired_lock)
    monkeypatch.setattr(persist_outcomes, "fetch_following_session_evidence", evidence)
    assert (
        await persist_outcomes._main(
            ["--from", "2026-08-01", "--to", "2026-08-02", "--dry-run", "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == payload["inserted"] == 0
    assert pool.closed is True


@pytest.mark.asyncio
async def test_outcome_cli_duplicate_lock_skips_reads_and_closes_pool(monkeypatch, capsys) -> None:
    pool = FakePool(object())

    async def connect(_settings):
        return pool

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("duplicate invocation read evidence")

    monkeypatch.setattr(persist_outcomes.DatabasePool, "connect", connect)
    monkeypatch.setattr(persist_outcomes, "DatabaseSettings", object)
    monkeypatch.setattr(persist_outcomes, "try_advisory_lock", unavailable_lock)
    monkeypatch.setattr(persist_outcomes, "fetch_following_session_evidence", forbidden)
    assert await persist_outcomes._main(["--from", "2026-08-01", "--to", "2026-08-02"]) == 0
    assert "holds the lock; skipping" in capsys.readouterr().out
    assert pool.closed is True


@pytest.mark.asyncio
async def test_study_cli_fake_database_full_staged_workflow(monkeypatch, capsys) -> None:
    pool = FakePool(object())
    raw_spec = json.loads(SPEC_PATH.read_text())
    plan = SimpleNamespace(specification=raw_spec)
    calls: list[str] = []

    async def connect(_settings):
        return pool

    async def register(_conn, spec, *, dry_run=False):
        calls.append(f"register:{dry_run}")
        return "inserted", {"specification_sha256": spec.digest}

    async def fetch_plan(*_args):
        return plan

    async def evaluate(_conn, _spec, *, stage, dry_run=False):
        calls.append(f"{stage}:{dry_run}")
        return "dry_run" if dry_run else "inserted", {"stage": stage}

    result = SimpleNamespace(
        stage="development",
        included_count=3,
        universe_count=4,
        decision="refine",
        decision_rationale="development only",
        cohort_sha256="a" * 64,
        rule_sha256="b" * 64,
        result_sha256="c" * 64,
        exclusions={"outcome_missing": 1},
        result_values={"difference": 1.0},
        frozen_rule={"threshold_minutes": 7.0},
        sample_gate_passed=False,
        coverage_gate_passed=False,
        complete_outcome_count=3,
        insufficient_outcome_count=1,
        assumptions=("observational",),
        limitations=("small sample",),
        quality_disclosures=("missing evidence disclosed",),
        failure_modes=("late capture",),
    )
    detail = SimpleNamespace(
        plan=SimpleNamespace(
            study_key="delay-retention-example",
            version=1,
            title="Example",
            hypothesis="Earlier detections retain more.",
            feature_version="earnings-reaction-v1",
            detector_version="fixed-preclose-2pct-v1",
            classifier_version="path-retention-v1",
            outcome_version="following-session-v1",
            development_from="2025-01-01",
            development_to="2025-12-31",
            minimum_development_sample=10,
            oos_from="2026-01-01",
            oos_to="2026-06-30",
            minimum_oos_sample=5,
            planned_analysis="Freeze a development threshold and apply it to OOS.",
            execution_assumptions="none",
            known_limitations=("small sample",),
            specification={"failure_modes": ["late capture"]},
            specification_sha256="d" * 64,
        ),
        results=(result,),
        members=(),
    )

    async def fetch_detail(*_args):
        return detail

    monkeypatch.setattr(study_cli.DatabasePool, "connect", connect)
    monkeypatch.setattr(study_cli, "DatabaseSettings", object)
    monkeypatch.setattr(study_cli, "try_advisory_lock", acquired_lock)
    monkeypatch.setattr(study_cli, "register_study", register)
    monkeypatch.setattr(study_cli, "fetch_study_version", fetch_plan)
    monkeypatch.setattr(study_cli, "evaluate_study", evaluate)
    monkeypatch.setattr(study_cli, "fetch_study_detail", fetch_detail)

    assert await study_cli._main(["register", "--spec", str(SPEC_PATH)]) == 0
    assert (
        await study_cli._main(
            [
                "evaluate-development",
                "--study",
                "delay-retention-example",
                "--version",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )
    assert (
        await study_cli._main(
            ["evaluate-oos", "--study", "delay-retention-example", "--version", "1"]
        )
        == 0
    )
    assert (
        await study_cli._main(
            ["show", "--study", "delay-retention-example", "--version", "1"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "decision_rationale=development only" in output
    assert "failure_modes=" in output
    assert calls == ["register:False", "development:True", "oos:False"]
    assert pool.closed is True

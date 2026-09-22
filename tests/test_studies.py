from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import event_row
from pydantic import ValidationError

from afterhours_lab.canonical import canonical_digest, canonical_json
from afterhours_lab.research import (
    EventFilter,
    EventSummary,
    FollowingSessionOutcome,
    canonical_filter_record,
)
from afterhours_lab.studies import (
    RULE_SCHEMA,
    DetectionDelaySplitRule,
    StudySpec,
    cohort_digest,
    evaluate_study,
    materialize_stage,
    member_digest,
    register_study,
)


def _spec() -> dict:
    return {
        "schema_version": "afterhours-study-spec-v1",
        "study_key": "delay-retention",
        "version": 1,
        "title": "Detection delay and following close",
        "hypothesis": "Earlier reactions have higher following-session close returns.",
        "owner": "research",
        "created_at": "2026-08-30T12:00:00-07:00",
        "filter": {
            "date_from": "2025-01-01",
            "date_to": "2026-06-30",
            "limit": 5,
            "offset": 4,
            "order_by": "symbol_asc",
        },
        "versions": {
            "feature": "earnings-reaction-v1",
            "detector": "fixed-preclose-2pct-v1",
            "classifier": "path-retention-v1",
            "outcome": "following-session-v1",
        },
        "development": {"from": "2025-01-01", "to": "2025-12-31"},
        "oos": {"from": "2026-01-01", "to": "2026-06-30"},
        "primary_metric": "following_session_close_return",
        "expected_direction": "higher",
        "analysis": {
            "name": "detection_delay_split_following_close_return-v1",
            "description": "Freeze the median development delay and compare early with late.",
        },
        "minimum_samples": {"development": 10, "oos": 5},
        "execution_assumptions": "none",
        "assumptions": ["observational association only"],
        "known_limitations": ["small samples may be unstable"],
        "failure_modes": ["missing authoritative outcomes"],
    }


def test_strict_spec_rejects_unknown_fields_and_overlap() -> None:
    payload = _spec()
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        StudySpec.model_validate(payload)
    payload = _spec()
    payload["oos"] = {"from": "2025-12-01", "to": "2026-06-30"}
    with pytest.raises(ValidationError, match="development must end"):
        StudySpec.model_validate(payload)


def test_spec_digest_is_stable_and_filter_omits_presentation_state() -> None:
    spec = StudySpec.model_validate(_spec())
    assert spec.digest == StudySpec.model_validate(_spec()).digest
    record = canonical_filter_record(spec.event_filter())
    assert not {"sort", "order_by", "page", "offset", "limit"} & record.keys()


def test_filter_canonicalization_orders_symbols() -> None:
    first = canonical_filter_record(EventFilter(symbols=("MSFT", "AAPL")))
    second = canonical_filter_record(EventFilter(symbols=("AAPL", "MSFT")))
    assert first == second


def test_canonical_json_rejects_nonfinite_and_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="NaN"):
        canonical_json({"value": float("nan")})
    with pytest.raises(ValueError, match="timezone"):
        canonical_json({"at": dt.datetime(2026, 8, 30, 12)})


def test_naive_preregistration_timestamp_is_rejected() -> None:
    payload = _spec()
    payload["created_at"] = dt.datetime(2026, 8, 30, 12)
    with pytest.raises(ValidationError, match="timezone"):
        StudySpec.model_validate(payload)


def test_frozen_rule_is_strict_typed_data() -> None:
    with pytest.raises(ValidationError):
        DetectionDelaySplitRule.model_validate(
            {
                "rule_type": "detection_delay_split-v1",
                "threshold_minutes": 17,
                "comparison": "early_if_lte",
                "metric": "following_session_close_return",
                "executable": "import os",
            }
        )
    with pytest.raises(ValidationError):
        DetectionDelaySplitRule(
            rule_type="detection_delay_split-v1",
            threshold_minutes=float("nan"),
            comparison="early_if_lte",
            metric="following_session_close_return",
        )


def test_member_and_cohort_digests_cover_decisions_evidence_and_versions() -> None:
    member = {
        "symbol": "TEST",
        "earnings_date": dt.date(2026, 1, 2),
        "included": True,
        "exclusion_reasons": [],
        "feature_version": "f1",
        "feature_evidence_sha256": "a" * 64,
        "outcome_version": "o1",
        "outcome_evidence_sha256": "b" * 64,
    }
    original = member_digest(member)
    for key, value in (
        ("included", False),
        ("exclusion_reasons", ["outcome_missing"]),
        ("feature_version", "f2"),
        ("outcome_evidence_sha256", "c" * 64),
    ):
        changed = {**member, key: value}
        assert member_digest(changed) != original
    with_digest = {**member, "member_sha256": original}
    assert cohort_digest([with_digest]) != cohort_digest(
        [{**with_digest, "outcome_status": "insufficient_data"}]
    )


@pytest.mark.asyncio
async def test_materialized_members_freeze_complete_universe_and_exclusions(monkeypatch) -> None:
    spec_payload = _spec()
    spec_payload["minimum_samples"] = {"development": 1, "oos": 1}
    spec = StudySpec.model_validate(spec_payload)
    included = EventSummary.from_row(event_row(earnings_date=dt.date(2025, 6, 1)))
    excluded = EventSummary.from_row(
        event_row(symbol="MISS", earnings_date=dt.date(2025, 6, 2))
    )
    outcome = FollowingSessionOutcome(
        symbol="TEST",
        earnings_date=included.earnings_date,
        outcome_version=spec.versions.outcome,
        analysis_status="complete",
        analysis_status_reason=None,
        following_session_close_return=2.5,
        source_evidence_sha256="d" * 64,
        missing_fields=(),
        data_quality_flags=(),
        coverage_identities={},
        values={},
    )

    async def cohort(*_args, **_kwargs):
        return SimpleNamespace(rows=(included, excluded))

    async def outcomes(*_args, **_kwargs):
        return (outcome,)

    monkeypatch.setattr("afterhours_lab.studies.fetch_cohort", cohort)
    monkeypatch.setattr("afterhours_lab.studies.fetch_following_session_outcomes", outcomes)
    materialized = await materialize_stage(object(), spec, "development")
    assert len(materialized["members"]) == 2
    missing = next(item for item in materialized["members"] if item["symbol"] == "MISS")
    assert missing["included"] is False
    assert missing["exclusion_reasons"] == ["outcome_missing"]


@pytest.mark.asyncio
async def test_result_and_members_roll_back_together(monkeypatch) -> None:
    spec = StudySpec.model_validate(_spec())
    plan = SimpleNamespace(specification_sha256=spec.digest)
    materialized = {
        "canonical": {
            "cohort_sha256": "a" * 64,
            "counts": {
                "universe": 1,
                "included": 1,
                "excluded": 0,
                "complete_outcome": 1,
                "insufficient_outcome": 0,
            },
            "exclusions": {},
            "metric_name": spec.primary_metric,
            "result_values": {"difference": 1.0},
            "sample_statistics": {"total_n": 1},
            "units": "percent",
            "frozen_rule": {
                "rule_type": "detection_delay_split-v1",
                "threshold_minutes": 1.0,
                "comparison": "early_if_lte",
                "metric": "following_session_close_return",
            },
            "rule_sha256": "b" * 64,
            "assumptions": ["observational"],
            "limitations": ["small sample"],
            "quality_disclosures": ["missing disclosed"],
            "failure_modes": ["coverage gaps"],
            "sample_gate_passed": True,
            "coverage_gate_passed": True,
            "decision": "refine",
            "decision_rationale": "development only",
        },
        "result_sha256": "c" * 64,
        "members": [
            {
                "symbol": "TEST",
                "earnings_date": dt.date(2025, 6, 1),
                "included": True,
                "exclusion_reasons": [],
                "feature_version": spec.versions.feature,
                "detector_version": spec.versions.detector,
                "classifier_version": spec.versions.classifier,
                "feature_evidence_sha256": "d" * 64,
                "feature_status": "complete",
                "outcome_version": spec.versions.outcome,
                "outcome_evidence_sha256": "e" * 64,
                "outcome_status": "complete",
                "data_quality_flags": [],
                "member_sha256": "f" * 64,
            }
        ],
    }

    async def exact(*_args):
        return plan

    async def no_results(*_args):
        return ()

    async def materialize(*_args, **_kwargs):
        return materialized

    monkeypatch.setattr("afterhours_lab.studies.fetch_study_version", exact)
    monkeypatch.setattr("afterhours_lab.studies.fetch_study_results", no_results)
    monkeypatch.setattr("afterhours_lab.studies.materialize_stage", materialize)

    class FailingConnection:
        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.rolled_back = False

        @asynccontextmanager
        async def transaction(self):
            try:
                yield
            except Exception:
                self.persisted.clear()
                self.rolled_back = True
                raise

        async def execute(self, *_args):
            self.persisted.append("result")

        async def executemany(self, *_args):
            raise RuntimeError("member insert failed")

    conn = FailingConnection()
    with pytest.raises(RuntimeError, match="member insert failed"):
        await evaluate_study(conn, spec, stage="development")
    assert conn.rolled_back is True
    assert conn.persisted == []


@pytest.mark.asyncio
async def test_registration_is_idempotent_but_changed_content_fails(monkeypatch) -> None:
    spec = StudySpec.model_validate(_spec())

    async def exact(*_args):
        return SimpleNamespace(specification_sha256=spec.digest)

    monkeypatch.setattr("afterhours_lab.studies.fetch_study_version", exact)
    status, _ = await register_study(object(), spec)
    assert status == "already_present"

    async def changed(*_args):
        return SimpleNamespace(specification_sha256="0" * 64)

    monkeypatch.setattr("afterhours_lab.studies.fetch_study_version", changed)
    with pytest.raises(ValueError, match="different canonical content"):
        await register_study(object(), spec)


@pytest.mark.asyncio
async def test_oos_is_sealed_before_development(monkeypatch) -> None:
    spec = StudySpec.model_validate(_spec())

    async def plan(*_args):
        return SimpleNamespace(specification_sha256=spec.digest)

    async def no_results(*_args):
        return ()

    monkeypatch.setattr("afterhours_lab.studies.fetch_study_version", plan)
    monkeypatch.setattr("afterhours_lab.studies.fetch_study_results", no_results)
    with pytest.raises(ValueError, match="OOS is sealed"):
        await evaluate_study(object(), spec, stage="oos", dry_run=True)


@pytest.mark.asyncio
async def test_oos_receives_the_stored_rule_without_refitting(monkeypatch) -> None:
    spec = StudySpec.model_validate(_spec())
    rule = {
        "rule_type": "detection_delay_split-v1",
        "threshold_minutes": 17.0,
        "comparison": "early_if_lte",
        "metric": "following_session_close_return",
    }
    rule_sha = canonical_digest(RULE_SCHEMA, rule)
    development = SimpleNamespace(
        stage="development",
        result_generation=1,
        frozen_rule=rule,
        rule_sha256=rule_sha,
        result_sha256="a" * 64,
    )

    async def plan(*_args):
        return SimpleNamespace(specification_sha256=spec.digest)

    async def results(*_args):
        return (development,)

    received = []

    async def materialize(_conn, _spec, stage, rule=None):
        received.append(rule)
        return {
            "canonical": {"rule_sha256": rule_sha},
            "result_sha256": "b" * 64,
            "members": [],
        }

    monkeypatch.setattr("afterhours_lab.studies.fetch_study_version", plan)
    monkeypatch.setattr("afterhours_lab.studies.fetch_study_results", results)
    monkeypatch.setattr("afterhours_lab.studies.materialize_stage", materialize)
    status, _ = await evaluate_study(object(), spec, stage="oos", dry_run=True)
    assert status == "dry_run"
    assert received == [rule]

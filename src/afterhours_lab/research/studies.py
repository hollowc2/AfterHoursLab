"""Typed read-only access to immutable study plans, results, and members."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


@dataclasses.dataclass(frozen=True)
class StudyVersionRecord:
    study_key: str
    version: int
    title: str
    hypothesis: str
    owner: str
    created_at: dt.datetime
    canonical_filter: dict[str, Any]
    specification: dict[str, Any]
    specification_sha256: str
    feature_version: str
    detector_version: str
    classifier_version: str
    outcome_version: str
    development_from: dt.date
    development_to: dt.date
    oos_from: dt.date
    oos_to: dt.date
    minimum_development_sample: int
    minimum_oos_sample: int
    primary_metric: str
    expected_direction: str
    analysis_name: str
    planned_analysis: str
    execution_assumptions: str
    known_limitations: tuple[str, ...]

    @classmethod
    def from_row(cls, row) -> StudyVersionRecord:
        names = {field.name for field in dataclasses.fields(cls)}
        values = {name: row[name] for name in names}
        values["canonical_filter"] = dict(_json(values["canonical_filter"]))
        values["specification"] = dict(_json(values["specification"]))
        values["known_limitations"] = tuple(_json(values["known_limitations"]))
        return cls(**values)


@dataclasses.dataclass(frozen=True)
class StudyResultRecord:
    stage: str
    result_generation: int
    evaluated_at: dt.datetime
    cohort_sha256: str
    universe_count: int
    included_count: int
    excluded_count: int
    complete_outcome_count: int
    insufficient_outcome_count: int
    exclusions: dict[str, int]
    metric_name: str
    result_values: dict[str, Any]
    sample_statistics: dict[str, Any]
    units: str
    frozen_rule: dict[str, Any]
    rule_sha256: str
    development_result_generation: int | None
    sample_gate_passed: bool
    coverage_gate_passed: bool
    decision: str
    decision_rationale: str
    result_sha256: str
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    quality_disclosures: tuple[str, ...]
    failure_modes: tuple[str, ...]

    @classmethod
    def from_row(cls, row) -> StudyResultRecord:
        names = {field.name for field in dataclasses.fields(cls)}
        values = {name: row[name] for name in names}
        for name in ("exclusions", "result_values", "sample_statistics", "frozen_rule"):
            values[name] = dict(_json(values[name]))
        for name in ("assumptions", "limitations", "quality_disclosures", "failure_modes"):
            values[name] = tuple(_json(values[name]))
        return cls(**values)


@dataclasses.dataclass(frozen=True)
class StudyMemberRecord:
    stage: str
    result_generation: int
    symbol: str
    earnings_date: dt.date
    included: bool
    exclusion_reasons: tuple[str, ...]
    feature_evidence_sha256: str | None
    feature_status: str | None
    outcome_evidence_sha256: str | None
    outcome_status: str | None
    data_quality_flags: tuple[str, ...]
    member_sha256: str

    @classmethod
    def from_row(cls, row) -> StudyMemberRecord:
        return cls(
            **{
                field.name: (
                    tuple(row[field.name] or ())
                    if field.name in {"exclusion_reasons", "data_quality_flags"}
                    else row[field.name]
                )
                for field in dataclasses.fields(cls)
            }
        )


@dataclasses.dataclass(frozen=True)
class StudyDetail:
    plan: StudyVersionRecord
    results: tuple[StudyResultRecord, ...]
    members: tuple[StudyMemberRecord, ...]


async def fetch_study_version(conn, study_key: str, version: int) -> StudyVersionRecord | None:
    row = await conn.fetchrow(
        "SELECT * FROM study_versions WHERE study_key=$1 AND version=$2", study_key, version
    )
    return StudyVersionRecord.from_row(row) if row else None


async def fetch_study_results(conn, study_key: str, version: int) -> tuple[StudyResultRecord, ...]:
    rows = await conn.fetch(
        """SELECT * FROM study_results WHERE study_key=$1 AND study_version=$2
           ORDER BY result_generation, stage""",
        study_key,
        version,
    )
    return tuple(StudyResultRecord.from_row(row) for row in rows)


async def fetch_study_members(conn, study_key: str, version: int) -> tuple[StudyMemberRecord, ...]:
    rows = await conn.fetch(
        """SELECT stage, result_generation, symbol, earnings_date, included,
                  exclusion_reasons, feature_evidence_sha256, feature_status,
                  outcome_evidence_sha256, outcome_status, data_quality_flags, member_sha256
           FROM study_members WHERE study_key=$1 AND study_version=$2
           ORDER BY result_generation, earnings_date, symbol""",
        study_key,
        version,
    )
    return tuple(StudyMemberRecord.from_row(row) for row in rows)


async def fetch_study_detail(conn, study_key: str, version: int) -> StudyDetail | None:
    plan = await fetch_study_version(conn, study_key, version)
    if plan is None:
        return None
    return StudyDetail(
        plan=plan,
        results=await fetch_study_results(conn, study_key, version),
        members=await fetch_study_members(conn, study_key, version),
    )

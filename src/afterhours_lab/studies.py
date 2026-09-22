"""Immutable study registration and staged development/OOS evaluation."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from afterhours_lab.canonical import canonical_digest, canonical_json
from afterhours_lab.outcomes import OUTCOME_VERSION
from afterhours_lab.research import (
    EventFilter,
    FeatureVersions,
    canonical_filter_record,
    fetch_cohort,
    fetch_following_session_outcomes,
    fetch_study_results,
    fetch_study_version,
    refinement_exclusion_reasons,
)

STUDY_SPEC_SCHEMA = "afterhours-study-spec-v1"
MEMBER_SCHEMA = "afterhours-study-member-v1"
COHORT_SCHEMA = "afterhours-study-cohort-v1"
RULE_SCHEMA = "detection-delay-split-rule-v1"
RESULT_SCHEMA = "afterhours-study-result-v1"
ANALYSIS_NAME = "detection_delay_split_following_close_return-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionsSpec(StrictModel):
    feature: str
    detector: str
    classifier: str
    outcome: str = OUTCOME_VERSION

    @field_validator("feature", "detector", "classifier", "outcome")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("version must be non-empty")
        return value


class PeriodSpec(StrictModel):
    from_date: dt.date = Field(alias="from")
    to_date: dt.date = Field(alias="to")

    @model_validator(mode="after")
    def ordered(self) -> PeriodSpec:
        if self.from_date > self.to_date:
            raise ValueError("period from must be on or before to")
        return self


class MinimumSamples(StrictModel):
    development: int = Field(gt=0)
    oos: int = Field(gt=0)


class AnalysisSpec(StrictModel):
    name: Literal["detection_delay_split_following_close_return-v1"]
    description: str = Field(min_length=1)


class DetectionDelaySplitRule(StrictModel):
    rule_type: Literal["detection_delay_split-v1"]
    threshold_minutes: float = Field(ge=0)
    comparison: Literal["early_if_lte"]
    metric: Literal["following_session_close_return"]

    @field_validator("threshold_minutes")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("threshold_minutes must be finite")
        return value


class StudySpec(StrictModel):
    schema_version: Literal["afterhours-study-spec-v1"] = STUDY_SPEC_SCHEMA
    study_key: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: int = Field(gt=0)
    title: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    created_at: dt.datetime
    filter: dict[str, Any]
    versions: VersionsSpec
    development: PeriodSpec
    oos: PeriodSpec
    primary_metric: Literal["following_session_close_return"]
    expected_direction: Literal["higher", "lower"]
    analysis: AnalysisSpec
    minimum_samples: MinimumSamples
    execution_assumptions: Literal["none"]
    assumptions: tuple[str, ...]
    known_limitations: tuple[str, ...]
    failure_modes: tuple[str, ...]

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("title", "hypothesis", "owner")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @field_validator("assumptions", "known_limitations", "failure_modes")
    @classmethod
    def nonempty_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("disclosure lists require non-empty strings")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def sealed_periods(self) -> StudySpec:
        if not (self.development.to_date < self.oos.from_date):
            raise ValueError("development must end before the OOS period begins")
        event_filter = self.event_filter()
        if event_filter.date_from and event_filter.date_from > self.development.from_date:
            raise ValueError("development period must fit the declared universe")
        if event_filter.date_to and event_filter.date_to < self.oos.to_date:
            raise ValueError("OOS period must fit the declared universe")
        return self

    def event_filter(self) -> EventFilter:
        allowed = {field.name for field in dataclasses.fields(EventFilter)}
        unknown = set(self.filter) - allowed
        if unknown:
            raise ValueError(f"unknown filter fields: {sorted(unknown)}")
        values = dict(self.filter)
        for name in ("date_from", "date_to"):
            if isinstance(values.get(name), str):
                values[name] = dt.date.fromisoformat(values[name])
        for name in (
            "symbols",
            "analysis_statuses",
            "reaction_classes",
            "directions",
            "postmarket_collection_modes",
        ):
            if name in values:
                if not isinstance(values[name], list | tuple):
                    raise ValueError(f"filter {name} must be an array")
                values[name] = tuple(values[name])
        values.pop("versions", None)
        values["versions"] = FeatureVersions(
            feature_version=self.versions.feature,
            detector_version=self.versions.detector,
            classifier_version=self.versions.classifier,
        )
        values["order_by"] = "earnings_date_asc"
        values["limit"] = None
        values["offset"] = 0
        return EventFilter(**values)

    def canonical_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python", by_alias=True)
        record["filter"] = canonical_filter_record(self.event_filter())
        return record

    @property
    def digest(self) -> str:
        return canonical_digest(STUDY_SPEC_SCHEMA, self.canonical_record())


def load_study_spec(path: Path) -> StudySpec:
    return StudySpec.model_validate_json(path.read_text(encoding="utf-8"))


def study_plan_row(spec: StudySpec) -> dict[str, Any]:
    canonical = spec.canonical_record()
    return {
        "study_key": spec.study_key,
        "version": spec.version,
        "title": spec.title,
        "hypothesis": spec.hypothesis,
        "owner": spec.owner,
        "created_at": spec.created_at,
        "canonical_filter": canonical["filter"],
        "feature_version": spec.versions.feature,
        "detector_version": spec.versions.detector,
        "classifier_version": spec.versions.classifier,
        "outcome_version": spec.versions.outcome,
        "development_from": spec.development.from_date,
        "development_to": spec.development.to_date,
        "oos_from": spec.oos.from_date,
        "oos_to": spec.oos.to_date,
        "primary_metric": spec.primary_metric,
        "expected_direction": spec.expected_direction,
        "analysis_name": spec.analysis.name,
        "planned_analysis": spec.analysis.description,
        "minimum_development_sample": spec.minimum_samples.development,
        "minimum_oos_sample": spec.minimum_samples.oos,
        "execution_assumptions": spec.execution_assumptions,
        "known_limitations": list(spec.known_limitations),
        "specification": canonical,
        "specification_sha256": spec.digest,
    }


async def register_study(
    conn, spec: StudySpec, *, dry_run: bool = False
) -> tuple[str, dict[str, Any]]:
    row = study_plan_row(spec)
    if dry_run:
        return "dry_run", row
    existing = await fetch_study_version(conn, spec.study_key, spec.version)
    if existing:
        if existing.specification_sha256 != spec.digest:
            raise ValueError("study key/version already exists with different canonical content")
        return "already_present", row
    columns = tuple(row)
    sql = f"""INSERT INTO study_versions ({", ".join(columns)})
              VALUES ({", ".join(f"${i}" for i in range(1, len(columns) + 1))})"""
    values = [
        canonical_json(row[name])
        if name in {"canonical_filter", "known_limitations", "specification"}
        else row[name]
        for name in columns
    ]
    async with conn.transaction():
        await conn.execute(sql, *values)
    return "inserted", row


def _universe_filter(event_filter: EventFilter, start: dt.date, end: dt.date) -> EventFilter:
    defaults = EventFilter()
    changes = {
        field.name: getattr(defaults, field.name)
        for field in dataclasses.fields(EventFilter)
        if field.name
        not in {"date_from", "date_to", "symbols", "versions", "order_by", "limit", "offset"}
    }
    return dataclasses.replace(
        event_filter,
        **changes,
        date_from=start,
        date_to=end,
        order_by="earnings_date_asc",
        limit=None,
        offset=0,
    )


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def member_digest(member: dict[str, Any]) -> str:
    payload = {key: value for key, value in member.items() if key != "member_sha256"}
    return canonical_digest(MEMBER_SCHEMA, payload)


def cohort_digest(members: list[dict[str, Any]]) -> str:
    ordered = sorted(members, key=lambda item: (item["earnings_date"], item["symbol"]))
    return canonical_digest(COHORT_SCHEMA, ordered)


async def materialize_stage(
    conn, spec: StudySpec, stage: Literal["development", "oos"], rule: dict[str, Any] | None = None
) -> dict[str, Any]:
    period = spec.development if stage == "development" else spec.oos
    selected_filter = dataclasses.replace(
        spec.event_filter(),
        date_from=period.from_date,
        date_to=period.to_date,
        order_by="earnings_date_asc",
        limit=None,
        offset=0,
    )
    universe = await fetch_cohort(
        conn, _universe_filter(selected_filter, period.from_date, period.to_date)
    )
    outcomes = await fetch_following_session_outcomes(
        conn,
        from_date=period.from_date,
        to_date=period.to_date,
        outcome_version=spec.versions.outcome,
    )
    outcome_groups: dict[tuple[str, dt.date], list[Any]] = {}
    for outcome in outcomes:
        outcome_groups.setdefault((outcome.symbol, outcome.earnings_date), []).append(outcome)
    chosen = {
        key: sorted(
            items,
            key=lambda item: (item.analysis_status != "complete", item.source_evidence_sha256),
        )[0]
        for key, items in outcome_groups.items()
    }
    members = []
    usable = []
    for event in universe.rows:
        reasons = list(refinement_exclusion_reasons(event, selected_filter))
        if event.analysis_status != "complete" or event.detection_delay_minutes is None:
            reasons.append("feature_not_complete")
        outcome = chosen.get((event.symbol, event.earnings_date))
        if outcome is None:
            reasons.append("outcome_missing")
        elif (
            outcome.analysis_status != "complete" or outcome.following_session_close_return is None
        ):
            reasons.append("outcome_not_complete")
        reasons = list(dict.fromkeys(reasons))
        payload = {
            "symbol": event.symbol,
            "earnings_date": event.earnings_date,
            "included": not reasons,
            "exclusion_reasons": sorted(reasons),
            "feature_version": spec.versions.feature,
            "detector_version": spec.versions.detector,
            "classifier_version": spec.versions.classifier,
            "feature_evidence_sha256": event.source_evidence_sha256,
            "feature_status": event.analysis_status,
            "outcome_version": spec.versions.outcome,
            "outcome_evidence_sha256": outcome.source_evidence_sha256 if outcome else None,
            "outcome_status": outcome.analysis_status if outcome else None,
            "data_quality_flags": sorted(
                set(event.data_quality_flags + (outcome.data_quality_flags if outcome else ()))
            ),
        }
        payload["member_sha256"] = member_digest(payload)
        members.append(payload)
        if not reasons:
            usable.append((event, outcome))
    if not usable:
        raise ValueError(f"{stage} has no complete feature/outcome rows from which to evaluate")
    if stage == "development":
        threshold = float(statistics.median(event.detection_delay_minutes for event, _ in usable))
        rule = DetectionDelaySplitRule(
            rule_type="detection_delay_split-v1",
            threshold_minutes=threshold,
            comparison="early_if_lte",
            metric=spec.primary_metric,
        ).model_dump(mode="python")
    if not rule:
        raise ValueError("a typed frozen development rule is required")
    rule = DetectionDelaySplitRule.model_validate(rule).model_dump(mode="python")
    threshold = float(rule["threshold_minutes"])
    early = [
        outcome.following_session_close_return
        for event, outcome in usable
        if event.detection_delay_minutes <= threshold
    ]
    late = [
        outcome.following_session_close_return
        for event, outcome in usable
        if event.detection_delay_minutes > threshold
    ]
    early_mean, late_mean = _mean(early), _mean(late)
    difference = (
        early_mean - late_mean if early_mean is not None and late_mean is not None else None
    )
    exclusions = Counter(reason for member in members for reason in member["exclusion_reasons"])
    member_records = sorted(members, key=lambda item: (item["earnings_date"], item["symbol"]))
    cohort_sha = cohort_digest(member_records)
    minimum = (
        spec.minimum_samples.development if stage == "development" else spec.minimum_samples.oos
    )
    sample_gate = len(usable) >= minimum and bool(early) and bool(late)
    eligible = sum(
        not refinement_exclusion_reasons(event, selected_filter)
        and event.analysis_status == "complete"
        for event in universe.rows
    )
    coverage_gate = eligible == len(usable)
    if stage == "development":
        decision = "refine"
        rationale = (
            "development evidence freezes the rule; promotion requires sealed OOS evaluation"
        )
    elif not sample_gate or not coverage_gate:
        decision = "reject"
        rationale = "declared sample or authoritative outcome coverage gate was not met"
    else:
        expected = difference is not None and (
            (difference > 0) if spec.expected_direction == "higher" else (difference < 0)
        )
        decision = "paper_trade_candidate" if expected else "reject"
        rationale = (
            "OOS direction matched the preregistration"
            if expected
            else "OOS direction did not match the preregistration"
        )
    result_values = {"early_mean": early_mean, "late_mean": late_mean, "difference": difference}
    canonical = {
        "study_key": spec.study_key,
        "study_version": spec.version,
        "stage": stage,
        "cohort_sha256": cohort_sha,
        "counts": {
            "universe": len(members),
            "included": len(usable),
            "excluded": len(members) - len(usable),
            "complete_outcome": sum(member["outcome_status"] == "complete" for member in members),
            "insufficient_outcome": sum(
                member["outcome_status"] == "insufficient_data" for member in members
            ),
        },
        "exclusions": dict(sorted(exclusions.items())),
        "metric_name": spec.primary_metric,
        "result_values": result_values,
        "sample_statistics": {"early_n": len(early), "late_n": len(late), "total_n": len(usable)},
        "units": "percent",
        "frozen_rule": rule,
        "rule_sha256": canonical_digest(RULE_SCHEMA, rule),
        "assumptions": list(spec.assumptions),
        "limitations": list(spec.known_limitations),
        "quality_disclosures": [
            "missing evidence is excluded with a frozen reason",
            "observational; execution assumptions are none",
        ],
        "failure_modes": list(spec.failure_modes),
        "sample_gate_passed": sample_gate,
        "coverage_gate_passed": coverage_gate,
        "decision": decision,
        "decision_rationale": rationale,
    }
    return {
        "canonical": canonical,
        "result_sha256": canonical_digest(RESULT_SCHEMA, canonical),
        "members": member_records,
    }


async def evaluate_study(
    conn,
    spec: StudySpec,
    *,
    stage: Literal["development", "oos"],
    dry_run: bool = False,
    evaluated_at: dt.datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    plan = await fetch_study_version(conn, spec.study_key, spec.version)
    if plan is None or plan.specification_sha256 != spec.digest:
        raise ValueError("the exact immutable study plan must be registered first")
    prior = await fetch_study_results(conn, spec.study_key, spec.version)
    development = [result for result in prior if result.stage == "development"]
    if stage == "oos" and not development:
        raise ValueError("OOS is sealed until a development result freezes a rule")
    dev = development[-1] if development else None
    materialized = await materialize_stage(
        conn,
        spec,
        stage,
        rule=dev.frozen_rule if dev else None,
    )
    if dev and materialized["canonical"]["rule_sha256"] != dev.rule_sha256:
        raise ValueError("OOS rule digest differs from the frozen development rule")
    if any(
        result.stage == stage and result.result_sha256 == materialized["result_sha256"]
        for result in prior
    ):
        return "already_present", materialized
    if stage == "oos" and any(result.stage == "oos" for result in prior):
        raise ValueError("OOS has already been evaluated for this study version")
    if dry_run:
        return "dry_run", materialized
    generation = max((result.result_generation for result in prior), default=0) + 1
    canonical = materialized["canonical"]
    result_row = {
        "study_key": spec.study_key,
        "study_version": spec.version,
        "stage": stage,
        "result_generation": generation,
        "evaluated_at": evaluated_at or dt.datetime.now(dt.UTC),
        "cohort_sha256": canonical["cohort_sha256"],
        "universe_count": canonical["counts"]["universe"],
        "included_count": canonical["counts"]["included"],
        "excluded_count": canonical["counts"]["excluded"],
        "complete_outcome_count": canonical["counts"]["complete_outcome"],
        "insufficient_outcome_count": canonical["counts"]["insufficient_outcome"],
        "exclusions": canonical["exclusions"],
        "metric_name": canonical["metric_name"],
        "result_values": canonical["result_values"],
        "sample_statistics": canonical["sample_statistics"],
        "units": canonical["units"],
        "frozen_rule": canonical["frozen_rule"],
        "rule_sha256": canonical["rule_sha256"],
        "development_result_generation": dev.result_generation if dev else None,
        "assumptions": canonical["assumptions"],
        "limitations": canonical["limitations"],
        "quality_disclosures": canonical["quality_disclosures"],
        "failure_modes": canonical["failure_modes"],
        "sample_gate_passed": canonical["sample_gate_passed"],
        "coverage_gate_passed": canonical["coverage_gate_passed"],
        "decision": canonical["decision"],
        "decision_rationale": canonical["decision_rationale"],
        "canonical_result": canonical,
        "result_sha256": materialized["result_sha256"],
    }
    json_fields = {
        "exclusions",
        "result_values",
        "sample_statistics",
        "frozen_rule",
        "assumptions",
        "limitations",
        "quality_disclosures",
        "failure_modes",
        "canonical_result",
    }
    async with conn.transaction():
        columns = tuple(result_row)
        result_placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        await conn.execute(
            f"INSERT INTO study_results ({', '.join(columns)}) VALUES ({result_placeholders})",
            *[
                canonical_json(result_row[name]) if name in json_fields else result_row[name]
                for name in columns
            ],
        )
        member_columns = (
            "study_key",
            "study_version",
            "stage",
            "result_generation",
            "symbol",
            "earnings_date",
            "included",
            "exclusion_reasons",
            "feature_version",
            "detector_version",
            "classifier_version",
            "feature_evidence_sha256",
            "feature_status",
            "outcome_version",
            "outcome_evidence_sha256",
            "outcome_status",
            "data_quality_flags",
            "member_sha256",
        )
        values = []
        for member in materialized["members"]:
            full = {
                "study_key": spec.study_key,
                "study_version": spec.version,
                "stage": stage,
                "result_generation": generation,
                **member,
            }
            values.append(tuple(full[name] for name in member_columns))
        member_placeholders = ", ".join(f"${i}" for i in range(1, len(member_columns) + 1))
        member_sql = (
            f"INSERT INTO study_members ({', '.join(member_columns)}) "
            f"VALUES ({member_placeholders})"
        )
        await conn.executemany(member_sql, values)
    return "inserted", materialized

"""Safe CLI for immutable preregistration, development, OOS, and inspection."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import sys
from pathlib import Path
from typing import Any

from afterhours_lab.canonical import canonical_json
from afterhours_lab.db.advisory_lock import try_advisory_lock
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.research import fetch_study_detail, fetch_study_version
from afterhours_lab.studies import StudySpec, evaluate_study, load_study_spec, register_study


def _lock_key(study_key: str, version: int) -> int:
    raw = hashlib.sha256(f"afterhours-study:{study_key}:{version}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Immutable staged AfterHoursLab studies")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--spec", required=True, type=Path)
    register.add_argument("--dry-run", action="store_true")
    for name in ("evaluate-development", "evaluate-oos", "show"):
        command = commands.add_parser(name)
        command.add_argument("--study", required=True)
        command.add_argument("--version", required=True, type=int)
        if name.startswith("evaluate"):
            command.add_argument("--dry-run", action="store_true")
        else:
            command.add_argument("--format", choices=("human", "json"), default="human")
    return parser.parse_args(argv)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "register":
        spec = load_study_spec(args.spec)
        if args.dry_run:
            status, row = await register_study(None, spec, dry_run=True)
            print(canonical_json({"status": status, "plan": row}))
            return 0
        study_key, version = spec.study_key, spec.version
    else:
        study_key, version = args.study, args.version
        spec = None

    pool = await DatabasePool.connect(DatabaseSettings())
    try:
        async with pool.acquire() as conn:
            if args.command == "show":
                detail = await fetch_study_detail(conn, study_key, version)
                if detail is None:
                    raise ValueError("study version not found")
                payload = _jsonable(detail)
                if args.format == "json":
                    print(canonical_json(payload))
                else:
                    print(f"{detail.plan.study_key} v{detail.plan.version}: {detail.plan.title}")
                    print(f"Hypothesis: {detail.plan.hypothesis}")
                    print(
                        f"Versions: {detail.plan.feature_version}/{detail.plan.detector_version}/"
                        f"{detail.plan.classifier_version}/{detail.plan.outcome_version}"
                    )
                    print(
                        f"Development: {detail.plan.development_from}.."
                        f"{detail.plan.development_to} "
                        f"(minimum n={detail.plan.minimum_development_sample})"
                    )
                    print(
                        f"OOS: {detail.plan.oos_from}..{detail.plan.oos_to} "
                        f"(minimum n={detail.plan.minimum_oos_sample})"
                    )
                    print(f"Analysis: {detail.plan.planned_analysis}")
                    print(f"Execution assumptions: {detail.plan.execution_assumptions}")
                    print(
                        f"Plan limitations: {canonical_json(detail.plan.known_limitations)}"
                    )
                    print(
                        "Plan failure modes: "
                        f"{canonical_json(detail.plan.specification['failure_modes'])}"
                    )
                    print(f"Plan digest: {detail.plan.specification_sha256}")
                    for result in detail.results:
                        print(
                            f"{result.stage}: n={result.included_count}/{result.universe_count} "
                            f"decision={result.decision} cohort={result.cohort_sha256} "
                            f"rule={result.rule_sha256} result={result.result_sha256}"
                        )
                        print(f"  decision_rationale={result.decision_rationale}")
                        print(f"  exclusions={canonical_json(result.exclusions)}")
                        print(f"  values={canonical_json(result.result_values)}")
                        print(f"  frozen_rule={canonical_json(result.frozen_rule)}")
                        print(
                            f"  gates: sample={result.sample_gate_passed} "
                            f"coverage={result.coverage_gate_passed}; "
                            f"complete outcomes={result.complete_outcome_count}; "
                            f"insufficient outcomes={result.insufficient_outcome_count}"
                        )
                        print(f"  assumptions={canonical_json(result.assumptions)}")
                        print(f"  limitations={canonical_json(result.limitations)}")
                        print(f"  quality={canonical_json(result.quality_disclosures)}")
                        print(f"  failure_modes={canonical_json(result.failure_modes)}")
                return 0
            async with try_advisory_lock(conn, _lock_key(study_key, version)) as acquired:
                if not acquired:
                    print("another command holds this study lock; skipping")
                    return 0
                if args.command == "register":
                    status, payload = await register_study(conn, spec, dry_run=False)
                else:
                    plan = await fetch_study_version(conn, study_key, version)
                    if plan is None:
                        raise ValueError("study version not found")
                    spec = StudySpec.model_validate(plan.specification)
                    stage = "development" if args.command == "evaluate-development" else "oos"
                    status, payload = await evaluate_study(
                        conn, spec, stage=stage, dry_run=args.dry_run
                    )
    finally:
        await pool.close()
    print(
        canonical_json(
            {"status": status, "study": study_key, "version": version, "result": payload}
        )
    )
    return 0


def main() -> None:
    try:
        code = asyncio.run(_main(sys.argv[1:]))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()

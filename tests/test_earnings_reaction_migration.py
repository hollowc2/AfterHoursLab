from __future__ import annotations

import re

from afterhours_lab.db.migrate import discover_migrations

MIGRATION_VERSION = "007_earnings_reaction_features"


def _migration_sql() -> str:
    migrations = {migration.version: migration for migration in discover_migrations()}
    assert MIGRATION_VERSION in migrations
    return " ".join(migrations[MIGRATION_VERSION].sql.split())


def test_reaction_feature_migration_is_forward_only_and_versioned() -> None:
    sql = _migration_sql()

    assert "CREATE TABLE earnings_reaction_features" in sql
    assert "ALTER TABLE" not in sql
    assert "DROP " not in sql
    assert (
        "PRIMARY KEY ( symbol, earnings_date, feature_version, detector_version, "
        "classifier_version )" in sql
    )
    assert "FOREIGN KEY (symbol, earnings_date) REFERENCES earnings_events" in sql
    assert "feature_version TEXT NOT NULL" in sql
    assert "algorithm_name TEXT NOT NULL" in sql
    assert "detector_version TEXT NOT NULL" in sql
    assert "classifier_version TEXT NOT NULL" in sql
    assert "parameters JSONB NOT NULL" in sql
    assert "source_evidence_sha256 TEXT NOT NULL" in sql
    assert "computed_at TIMESTAMPTZ NOT NULL DEFAULT now()" in sql


def test_reaction_feature_migration_persists_fixed_horizon_features() -> None:
    sql = _migration_sql()

    for minutes in (1, 5, 15, 30, 60, 105):
        assert f"return_{minutes}m DOUBLE PRECISION" in sql
        assert f"volume_{minutes}m BIGINT" in sql

    for field in (
        "reaction_timestamp TIMESTAMPTZ",
        "detection_delay_minutes INTEGER",
        "reference_price DOUBLE PRECISION",
        "initial_return DOUBLE PRECISION",
        "window_high_return DOUBLE PRECISION",
        "window_low_return DOUBLE PRECISION",
        "max_favorable_excursion DOUBLE PRECISION",
        "max_adverse_excursion DOUBLE PRECISION",
        "max_retracement DOUBLE PRECISION",
        "reaction_vwap_proxy DOUBLE PRECISION",
    ):
        assert field in sql


def test_reaction_outcomes_and_missing_data_are_unambiguous() -> None:
    sql = _migration_sql()

    for status in ("complete", "no_trigger_in_window", "insufficient_data"):
        assert f"'{status}'" in sql
    for reaction_class in (
        "immediate_continuation",
        "spike_and_fade",
        "delayed_breakout",
        "whipsaw",
        "no_trigger_in_window",
    ):
        assert f"'{reaction_class}'" in sql

    assert "missing_fields TEXT[] NOT NULL DEFAULT '{}'" in sql
    assert "data_quality_flags TEXT[] NOT NULL DEFAULT '{}'" in sql
    assert "analysis_status_reason TEXT" in sql
    assert re.search(
        r"analysis_status = 'insufficient_data'.*?reaction_class IS NULL.*?"
        r"reaction_direction IS NULL.*?reaction_timestamp IS NULL.*?"
        r"detection_delay_minutes IS NULL.*?initial_return IS NULL.*?"
        r"max_favorable_excursion IS NULL.*?max_adverse_excursion IS NULL.*?"
        r"max_retracement IS NULL.*?"
        r"analysis_status_reason IS NOT NULL.*?"
        r"cardinality\(missing_fields\) > 0",
        sql,
    )
    assert re.search(
        r"analysis_status = 'no_trigger_in_window'.*?"
        r"reaction_class = 'no_trigger_in_window'.*?"
        r"reaction_direction = 'flat'.*?reaction_timestamp IS NULL.*?"
        r"max_retracement IS NULL.*?num_nonnulls\(.*?\) = 16",
        sql,
    )


def test_reaction_feature_migration_guards_provenance_and_ranges() -> None:
    sql = _migration_sql()

    assert "source_bar_count INTEGER NOT NULL CHECK (source_bar_count >= 0)" in sql
    assert "study_observed_minutes INTEGER NOT NULL" in sql
    assert "study_missing_minutes INTEGER NOT NULL" in sql
    assert "study_coverage_ratio DOUBLE PRECISION NOT NULL" in sql
    assert "earnings_regular_response_sha256 TEXT CHECK" in sql
    assert "earnings_regular_collection_mode TEXT CHECK" in sql
    assert "earnings_postmarket_response_sha256 TEXT CHECK" in sql
    assert "earnings_postmarket_collection_mode TEXT CHECK" in sql
    assert "source_evidence_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "jsonb_typeof(parameters) = 'object'" in sql
    assert "OR feature_window_end > feature_window_start" in sql
    assert "OR feature_cutoff_ts > feature_window_start" in sql
    assert "OR label_cutoff_ts >= feature_cutoff_ts" in sql
    assert "OR label_cutoff_ts <= feature_window_end" in sql
    assert (
        "reaction_timestamp BETWEEN feature_window_start AND feature_cutoff_ts" in sql
    )
    assert "num_nonnulls(" in sql
    assert "idx_earnings_reaction_features_date_status" in sql
    assert "idx_earnings_reaction_features_class" in sql

from pathlib import Path

SQL = (
    Path(__file__).parents[1] / "src/afterhours_lab/db/migrations/010_phase5_studies.sql"
).read_text()


def test_phase5_tables_and_append_only_triggers_are_present() -> None:
    for table in (
        "following_session_outcomes",
        "study_versions",
        "study_results",
        "study_members",
    ):
        assert f"CREATE TABLE {table}" in SQL
        assert f"CREATE TRIGGER {table}_immutable" in SQL
    assert "BEFORE UPDATE OR DELETE" in SQL


def test_outcome_key_allows_new_evidence_generations_without_overwrite() -> None:
    assert "PRIMARY KEY (symbol, earnings_date, outcome_version, source_evidence_sha256)" in SQL
    assert "not_yet_available" not in SQL


def test_schema_enforces_oos_seal_versions_and_promotion_gates() -> None:
    assert "CREATE TRIGGER study_oos_seal" in SQL
    assert "OOS rule digest must equal the frozen development rule" in SQL
    assert "CREATE TRIGGER study_member_versions" in SQL
    assert "decision <> 'paper_trade_candidate'" in SQL
    assert "sample_gate_passed AND coverage_gate_passed" in SQL

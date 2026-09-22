-- Phase 5A: versioned following-session labels and immutable staged studies.
CREATE TABLE following_session_outcomes (
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    outcome_version TEXT NOT NULL CHECK (btrim(outcome_version) <> ''),
    algorithm_name TEXT NOT NULL CHECK (btrim(algorithm_name) <> ''),
    analysis_status TEXT NOT NULL CHECK (analysis_status IN ('complete', 'insufficient_data')),
    analysis_status_reason TEXT,
    reference_close DOUBLE PRECISION CHECK (reference_close > 0),
    reference_close_ts TIMESTAMPTZ,
    premarket_first DOUBLE PRECISION CHECK (premarket_first > 0),
    premarket_high DOUBLE PRECISION CHECK (premarket_high > 0),
    premarket_low DOUBLE PRECISION CHECK (premarket_low > 0),
    premarket_last DOUBLE PRECISION CHECK (premarket_last > 0),
    regular_open DOUBLE PRECISION CHECK (regular_open > 0),
    regular_high DOUBLE PRECISION CHECK (regular_high > 0),
    regular_low DOUBLE PRECISION CHECK (regular_low > 0),
    regular_close DOUBLE PRECISION CHECK (regular_close > 0),
    premarket_first_return DOUBLE PRECISION,
    premarket_high_return DOUBLE PRECISION,
    premarket_low_return DOUBLE PRECISION,
    premarket_last_return DOUBLE PRECISION,
    regular_open_return DOUBLE PRECISION,
    regular_high_return DOUBLE PRECISION,
    regular_low_return DOUBLE PRECISION,
    regular_close_return DOUBLE PRECISION,
    regular_return_5m DOUBLE PRECISION,
    regular_return_30m DOUBLE PRECISION,
    regular_return_60m DOUBLE PRECISION,
    overnight_gap_return DOUBLE PRECISION,
    following_session_close_return DOUBLE PRECISION,
    regular_observed_minutes INTEGER NOT NULL CHECK (regular_observed_minutes >= 0),
    regular_expected_minutes INTEGER NOT NULL CHECK (regular_expected_minutes > 0),
    regular_coverage_ratio DOUBLE PRECISION NOT NULL CHECK (regular_coverage_ratio BETWEEN 0 AND 1),
    premarket_observed_minutes INTEGER NOT NULL CHECK (premarket_observed_minutes >= 0),
    premarket_expected_minutes INTEGER NOT NULL CHECK (premarket_expected_minutes > 0),
    premarket_coverage_ratio DOUBLE PRECISION NOT NULL CHECK (
        premarket_coverage_ratio BETWEEN 0 AND 1
    ),
    following_regular_observed_minutes INTEGER NOT NULL CHECK (
        following_regular_observed_minutes >= 0
    ),
    following_regular_expected_minutes INTEGER NOT NULL CHECK (
        following_regular_expected_minutes > 0
    ),
    following_regular_coverage_ratio DOUBLE PRECISION NOT NULL CHECK (
        following_regular_coverage_ratio BETWEEN 0 AND 1
    ),
    earnings_regular_response_sha256 TEXT CHECK (
        earnings_regular_response_sha256 IS NULL OR
        earnings_regular_response_sha256 ~ '^[0-9a-f]{64}$'
    ),
    earnings_regular_collection_mode TEXT CHECK (
        earnings_regular_collection_mode IS NULL OR
        earnings_regular_collection_mode IN ('scheduled_capture', 'historical_backfill')
    ),
    following_premarket_response_sha256 TEXT CHECK (
        following_premarket_response_sha256 IS NULL OR
        following_premarket_response_sha256 ~ '^[0-9a-f]{64}$'
    ),
    following_premarket_collection_mode TEXT CHECK (
        following_premarket_collection_mode IS NULL OR
        following_premarket_collection_mode IN ('scheduled_capture', 'historical_backfill')
    ),
    following_regular_response_sha256 TEXT CHECK (
        following_regular_response_sha256 IS NULL OR
        following_regular_response_sha256 ~ '^[0-9a-f]{64}$'
    ),
    following_regular_collection_mode TEXT CHECK (
        following_regular_collection_mode IS NULL OR
        following_regular_collection_mode IN ('scheduled_capture', 'historical_backfill')
    ),
    coverage_identities JSONB NOT NULL CHECK (jsonb_typeof(coverage_identities) = 'object'),
    source_evidence_sha256 TEXT NOT NULL CHECK (source_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    parameters JSONB NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
    outcome_values JSONB NOT NULL CHECK (jsonb_typeof(outcome_values) = 'object'),
    missing_fields TEXT[] NOT NULL DEFAULT '{}',
    data_quality_flags TEXT[] NOT NULL DEFAULT '{}',
    computed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, earnings_date, outcome_version, source_evidence_sha256),
    FOREIGN KEY (symbol, earnings_date) REFERENCES earnings_events (symbol, earnings_date),
    CHECK (
      (analysis_status='complete' AND analysis_status_reason IS NULL
       AND cardinality(missing_fields)=0
       AND num_nonnulls(reference_close, reference_close_ts, premarket_first, premarket_high,
           premarket_low, premarket_last, regular_open, regular_high, regular_low, regular_close,
           regular_return_5m, regular_return_30m, regular_return_60m)=13)
      OR
      (analysis_status='insufficient_data' AND analysis_status_reason IS NOT NULL
       AND cardinality(missing_fields)>0)
    )
    ,CHECK ((earnings_regular_response_sha256 IS NULL) =
            (earnings_regular_collection_mode IS NULL))
    ,CHECK ((following_premarket_response_sha256 IS NULL) =
            (following_premarket_collection_mode IS NULL))
    ,CHECK ((following_regular_response_sha256 IS NULL) =
            (following_regular_collection_mode IS NULL))
    ,CHECK (regular_observed_minutes <= regular_expected_minutes)
    ,CHECK (premarket_observed_minutes <= premarket_expected_minutes)
    ,CHECK (following_regular_observed_minutes <= following_regular_expected_minutes)
);

CREATE INDEX idx_following_outcomes_date_version
    ON following_session_outcomes (earnings_date, outcome_version, analysis_status);

CREATE TABLE study_versions (
    study_key TEXT NOT NULL CHECK (study_key ~ '^[a-z][a-z0-9-]{1,63}$'),
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    hypothesis TEXT NOT NULL CHECK (btrim(hypothesis) <> ''),
    owner TEXT NOT NULL CHECK (btrim(owner) <> ''),
    created_at TIMESTAMPTZ NOT NULL,
    canonical_filter JSONB NOT NULL CHECK (jsonb_typeof(canonical_filter)='object'),
    feature_version TEXT NOT NULL CHECK (btrim(feature_version) <> ''),
    detector_version TEXT NOT NULL CHECK (btrim(detector_version) <> ''),
    classifier_version TEXT NOT NULL CHECK (btrim(classifier_version) <> ''),
    outcome_version TEXT NOT NULL CHECK (btrim(outcome_version) <> ''),
    development_from DATE NOT NULL,
    development_to DATE NOT NULL,
    oos_from DATE NOT NULL,
    oos_to DATE NOT NULL,
    primary_metric TEXT NOT NULL CHECK (btrim(primary_metric) <> ''),
    expected_direction TEXT NOT NULL CHECK (expected_direction IN ('higher', 'lower')),
    analysis_name TEXT NOT NULL CHECK (btrim(analysis_name) <> ''),
    planned_analysis TEXT NOT NULL CHECK (btrim(planned_analysis) <> ''),
    minimum_development_sample INTEGER NOT NULL CHECK (minimum_development_sample > 0),
    minimum_oos_sample INTEGER NOT NULL CHECK (minimum_oos_sample > 0),
    execution_assumptions TEXT NOT NULL CHECK (execution_assumptions = 'none'),
    known_limitations JSONB NOT NULL CHECK (jsonb_typeof(known_limitations)='array'),
    specification JSONB NOT NULL CHECK (jsonb_typeof(specification)='object'),
    specification_sha256 TEXT NOT NULL CHECK (specification_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (study_key, version),
    UNIQUE (study_key, version, specification_sha256),
    CHECK (development_from <= development_to),
    CHECK (oos_from <= oos_to),
    CHECK (development_to < oos_from OR oos_to < development_from)
);

CREATE TABLE study_results (
    study_key TEXT NOT NULL,
    study_version INTEGER NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('development', 'oos')),
    result_generation INTEGER NOT NULL CHECK (result_generation > 0),
    evaluated_at TIMESTAMPTZ NOT NULL,
    cohort_sha256 TEXT NOT NULL CHECK (cohort_sha256 ~ '^[0-9a-f]{64}$'),
    universe_count INTEGER NOT NULL CHECK (universe_count >= 0),
    included_count INTEGER NOT NULL CHECK (included_count >= 0),
    excluded_count INTEGER NOT NULL CHECK (excluded_count >= 0),
    complete_outcome_count INTEGER NOT NULL CHECK (complete_outcome_count >= 0),
    insufficient_outcome_count INTEGER NOT NULL CHECK (insufficient_outcome_count >= 0),
    exclusions JSONB NOT NULL CHECK (jsonb_typeof(exclusions)='object'),
    metric_name TEXT NOT NULL CHECK (btrim(metric_name) <> ''),
    result_values JSONB NOT NULL CHECK (jsonb_typeof(result_values)='object'),
    sample_statistics JSONB NOT NULL CHECK (jsonb_typeof(sample_statistics)='object'),
    units TEXT NOT NULL CHECK (btrim(units) <> ''),
    frozen_rule JSONB NOT NULL CHECK (jsonb_typeof(frozen_rule)='object'),
    rule_sha256 TEXT NOT NULL CHECK (rule_sha256 ~ '^[0-9a-f]{64}$'),
    development_result_generation INTEGER,
    assumptions JSONB NOT NULL CHECK (jsonb_typeof(assumptions)='array'),
    limitations JSONB NOT NULL CHECK (jsonb_typeof(limitations)='array'),
    quality_disclosures JSONB NOT NULL CHECK (jsonb_typeof(quality_disclosures)='array'),
    failure_modes JSONB NOT NULL CHECK (jsonb_typeof(failure_modes)='array'),
    sample_gate_passed BOOLEAN NOT NULL,
    coverage_gate_passed BOOLEAN NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('reject', 'refine', 'paper_trade_candidate')),
    decision_rationale TEXT NOT NULL CHECK (btrim(decision_rationale) <> ''),
    canonical_result JSONB NOT NULL CHECK (jsonb_typeof(canonical_result)='object'),
    result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (study_key, study_version, stage, result_generation),
    UNIQUE (study_key, study_version, result_generation),
    UNIQUE (study_key, study_version, stage, result_sha256),
    FOREIGN KEY (study_key, study_version) REFERENCES study_versions (study_key, version),
    FOREIGN KEY (study_key, study_version, development_result_generation)
      REFERENCES study_results (study_key, study_version, result_generation)
      DEFERRABLE INITIALLY DEFERRED,
    CHECK (universe_count = included_count + excluded_count),
    CHECK ((stage='development' AND development_result_generation IS NULL)
        OR (stage='oos' AND development_result_generation IS NOT NULL)),
    CHECK (frozen_rule <> '{}'::jsonb),
    CHECK (jsonb_array_length(assumptions) > 0),
    CHECK (jsonb_array_length(limitations) > 0),
    CHECK (jsonb_array_length(failure_modes) > 0),
    CHECK (decision <> 'paper_trade_candidate' OR (sample_gate_passed AND coverage_gate_passed))
);

ALTER TABLE study_results ADD CONSTRAINT study_results_development_stage_check
    CHECK (development_result_generation IS NULL OR stage='oos');

CREATE FUNCTION enforce_study_oos_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE development_rule TEXT;
BEGIN
  IF NEW.stage = 'oos' THEN
    SELECT rule_sha256 INTO development_rule
      FROM study_results
     WHERE study_key=NEW.study_key AND study_version=NEW.study_version
       AND stage='development'
       AND result_generation=NEW.development_result_generation;
    IF development_rule IS NULL THEN
      RAISE EXCEPTION 'OOS requires an existing development result';
    END IF;
    IF development_rule <> NEW.rule_sha256 THEN
      RAISE EXCEPTION 'OOS rule digest must equal the frozen development rule';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER study_oos_seal BEFORE INSERT ON study_results
FOR EACH ROW EXECUTE FUNCTION enforce_study_oos_seal();

CREATE TABLE study_members (
    study_key TEXT NOT NULL,
    study_version INTEGER NOT NULL,
    stage TEXT NOT NULL,
    result_generation INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    included BOOLEAN NOT NULL,
    exclusion_reasons TEXT[] NOT NULL DEFAULT '{}',
    feature_version TEXT NOT NULL,
    detector_version TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    feature_evidence_sha256 TEXT,
    feature_status TEXT,
    outcome_version TEXT NOT NULL,
    outcome_evidence_sha256 TEXT,
    outcome_status TEXT,
    data_quality_flags TEXT[] NOT NULL DEFAULT '{}',
    member_sha256 TEXT NOT NULL CHECK (member_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (study_key, study_version, stage, result_generation, symbol, earnings_date),
    FOREIGN KEY (study_key, study_version, stage, result_generation)
      REFERENCES study_results (study_key, study_version, stage, result_generation)
      ON DELETE RESTRICT,
    FOREIGN KEY (symbol, earnings_date) REFERENCES earnings_events (symbol, earnings_date),
    CHECK ((included AND cardinality(exclusion_reasons)=0)
        OR (NOT included AND cardinality(exclusion_reasons)>0))
);

CREATE INDEX idx_study_members_event ON study_members (symbol, earnings_date);

CREATE FUNCTION enforce_study_member_versions() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pinned study_versions%ROWTYPE;
BEGIN
  SELECT * INTO pinned FROM study_versions
   WHERE study_key=NEW.study_key AND version=NEW.study_version;
  IF NEW.feature_version <> pinned.feature_version
     OR NEW.detector_version <> pinned.detector_version
     OR NEW.classifier_version <> pinned.classifier_version
     OR NEW.outcome_version <> pinned.outcome_version THEN
    RAISE EXCEPTION 'study member versions differ from the immutable plan';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER study_member_versions BEFORE INSERT ON study_members
FOR EACH ROW EXECUTE FUNCTION enforce_study_member_versions();

-- Tables are append-only even for ad-hoc SQL roles: corrections require a new generation.
CREATE FUNCTION reject_phase5_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$;
CREATE TRIGGER following_session_outcomes_immutable BEFORE UPDATE OR DELETE ON following_session_outcomes
FOR EACH ROW EXECUTE FUNCTION reject_phase5_mutation();
CREATE TRIGGER study_versions_immutable BEFORE UPDATE OR DELETE ON study_versions
FOR EACH ROW EXECUTE FUNCTION reject_phase5_mutation();
CREATE TRIGGER study_results_immutable BEFORE UPDATE OR DELETE ON study_results
FOR EACH ROW EXECUTE FUNCTION reject_phase5_mutation();
CREATE TRIGGER study_members_immutable BEFORE UPDATE OR DELETE ON study_members
FOR EACH ROW EXECUTE FUNCTION reject_phase5_mutation();

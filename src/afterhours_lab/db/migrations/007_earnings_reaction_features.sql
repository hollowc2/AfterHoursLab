-- Versioned, reproducible features for retrospective after-hours earnings studies.
-- The composite key lets recalculation under a new definition coexist with prior
-- work. Insert-only application behavior will be added with the persistence layer;
-- this schema does not claim to prevent an operator from issuing UPDATE or DELETE.
CREATE TABLE earnings_reaction_features (
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    feature_version TEXT NOT NULL CHECK (btrim(feature_version) <> ''),
    algorithm_name TEXT NOT NULL CHECK (btrim(algorithm_name) <> ''),
    detector_version TEXT NOT NULL CHECK (btrim(detector_version) <> ''),
    classifier_version TEXT NOT NULL CHECK (btrim(classifier_version) <> ''),
    analysis_status TEXT NOT NULL CHECK (analysis_status IN (
        'complete', 'no_trigger_in_window', 'insufficient_data'
    )),
    analysis_status_reason TEXT,
    reaction_class TEXT CHECK (reaction_class IN (
        'immediate_continuation', 'spike_and_fade',
        'delayed_breakout', 'whipsaw', 'no_trigger_in_window'
    )),
    reaction_direction TEXT CHECK (reaction_direction IN ('up', 'down', 'flat')),
    feature_window_start TIMESTAMPTZ,
    feature_window_end TIMESTAMPTZ,
    feature_cutoff_ts TIMESTAMPTZ,
    label_cutoff_ts TIMESTAMPTZ,
    reaction_timestamp TIMESTAMPTZ,
    detection_delay_minutes INTEGER CHECK (
        detection_delay_minutes IS NULL
        OR detection_delay_minutes BETWEEN 1 AND 105
    ),
    reference_price DOUBLE PRECISION CHECK (reference_price > 0),
    initial_return DOUBLE PRECISION,
    return_1m DOUBLE PRECISION,
    return_5m DOUBLE PRECISION,
    return_15m DOUBLE PRECISION,
    return_30m DOUBLE PRECISION,
    return_60m DOUBLE PRECISION,
    return_105m DOUBLE PRECISION,
    window_high_return DOUBLE PRECISION,
    window_low_return DOUBLE PRECISION,
    max_favorable_excursion DOUBLE PRECISION CHECK (
        max_favorable_excursion IS NULL OR max_favorable_excursion >= 0
    ),
    max_adverse_excursion DOUBLE PRECISION CHECK (
        max_adverse_excursion IS NULL OR max_adverse_excursion <= 0
    ),
    max_retracement DOUBLE PRECISION CHECK (
        max_retracement IS NULL OR max_retracement >= 0
    ),
    reaction_vwap_proxy DOUBLE PRECISION CHECK (
        reaction_vwap_proxy IS NULL OR reaction_vwap_proxy > 0
    ),
    volume_1m BIGINT CHECK (volume_1m IS NULL OR volume_1m >= 0),
    volume_5m BIGINT CHECK (volume_5m IS NULL OR volume_5m >= 0),
    volume_15m BIGINT CHECK (volume_15m IS NULL OR volume_15m >= 0),
    volume_30m BIGINT CHECK (volume_30m IS NULL OR volume_30m >= 0),
    volume_60m BIGINT CHECK (volume_60m IS NULL OR volume_60m >= 0),
    volume_105m BIGINT CHECK (volume_105m IS NULL OR volume_105m >= 0),
    source_bar_count INTEGER NOT NULL CHECK (source_bar_count >= 0),
    study_observed_minutes INTEGER NOT NULL CHECK (study_observed_minutes >= 0),
    study_missing_minutes INTEGER NOT NULL CHECK (study_missing_minutes >= 0),
    study_coverage_ratio DOUBLE PRECISION NOT NULL CHECK (
        study_coverage_ratio BETWEEN 0 AND 1
    ),
    earnings_regular_response_sha256 TEXT CHECK (
        earnings_regular_response_sha256 IS NULL OR
        earnings_regular_response_sha256 ~ '^[0-9a-f]{64}$'
    ),
    earnings_regular_collection_mode TEXT CHECK (
        earnings_regular_collection_mode IS NULL OR
        earnings_regular_collection_mode IN ('scheduled_capture', 'historical_backfill')
    ),
    earnings_postmarket_response_sha256 TEXT CHECK (
        earnings_postmarket_response_sha256 IS NULL OR
        earnings_postmarket_response_sha256 ~ '^[0-9a-f]{64}$'
    ),
    earnings_postmarket_collection_mode TEXT CHECK (
        earnings_postmarket_collection_mode IS NULL OR
        earnings_postmarket_collection_mode IN ('scheduled_capture', 'historical_backfill')
    ),
    source_evidence_sha256 TEXT NOT NULL CHECK (
        source_evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
    parameters JSONB NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
    missing_fields TEXT[] NOT NULL DEFAULT '{}',
    data_quality_flags TEXT[] NOT NULL DEFAULT '{}',
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        symbol, earnings_date,
        feature_version, detector_version, classifier_version
    ),
    FOREIGN KEY (symbol, earnings_date)
        REFERENCES earnings_events (symbol, earnings_date),
    CHECK (
        feature_window_start IS NULL OR feature_window_end IS NULL
        OR feature_window_end > feature_window_start
    ),
    CHECK (
        feature_window_start IS NULL OR feature_cutoff_ts IS NULL
        OR feature_cutoff_ts > feature_window_start
    ),
    CHECK (
        feature_cutoff_ts IS NULL OR label_cutoff_ts IS NULL
        OR label_cutoff_ts >= feature_cutoff_ts
    ),
    CHECK (
        label_cutoff_ts IS NULL OR feature_window_end IS NULL
        OR label_cutoff_ts <= feature_window_end
    ),
    CHECK (analysis_status_reason IS NULL OR btrim(analysis_status_reason) <> ''),
    CHECK (
        (earnings_regular_response_sha256 IS NULL)
        = (earnings_regular_collection_mode IS NULL)
    ),
    CHECK (
        (earnings_postmarket_response_sha256 IS NULL)
        = (earnings_postmarket_collection_mode IS NULL)
    ),
    CHECK (
        reaction_timestamp IS NULL
        OR reaction_timestamp BETWEEN feature_window_start AND feature_cutoff_ts
    ),
    CHECK (
        (analysis_status = 'complete'
            AND reaction_class IN (
                'immediate_continuation', 'spike_and_fade',
                'delayed_breakout', 'whipsaw'
            )
            AND reaction_direction IN ('up', 'down')
            AND reaction_timestamp IS NOT NULL
            AND detection_delay_minutes IS NOT NULL
            AND analysis_status_reason IS NULL
            AND feature_window_start IS NOT NULL
            AND feature_window_end IS NOT NULL
            AND feature_cutoff_ts IS NOT NULL
            AND label_cutoff_ts IS NOT NULL
            AND earnings_regular_response_sha256 IS NOT NULL
            AND earnings_postmarket_response_sha256 IS NOT NULL
            AND num_nonnulls(
                reference_price,
                initial_return,
                return_1m, return_5m, return_15m,
                return_30m, return_60m, return_105m,
                window_high_return, window_low_return,
                max_favorable_excursion, max_adverse_excursion, max_retracement,
                reaction_vwap_proxy,
                volume_1m, volume_5m, volume_15m,
                volume_30m, volume_60m, volume_105m
            ) = 20
            AND cardinality(missing_fields) = 0)
        OR
        (analysis_status = 'no_trigger_in_window'
            AND reaction_class = 'no_trigger_in_window'
            AND reaction_direction = 'flat'
            AND reaction_timestamp IS NULL
            AND detection_delay_minutes IS NULL
            AND initial_return IS NULL
            AND max_favorable_excursion IS NULL
            AND max_adverse_excursion IS NULL
            AND analysis_status_reason IS NOT NULL
            AND feature_window_start IS NOT NULL
            AND feature_window_end IS NOT NULL
            AND feature_cutoff_ts IS NOT NULL
            AND label_cutoff_ts IS NOT NULL
            AND earnings_regular_response_sha256 IS NOT NULL
            AND earnings_postmarket_response_sha256 IS NOT NULL
            -- There is no post-signal path when no close-confirmed trigger exists,
            -- so retracement is intentionally NULL while fixed-window high/low,
            -- horizons, volume, and the VWAP proxy remain required.
            AND max_retracement IS NULL
            AND num_nonnulls(
                reference_price,
                initial_return,
                return_1m, return_5m, return_15m,
                return_30m, return_60m, return_105m,
                window_high_return, window_low_return,
                max_favorable_excursion, max_adverse_excursion, max_retracement,
                reaction_vwap_proxy,
                volume_1m, volume_5m, volume_15m,
                volume_30m, volume_60m, volume_105m
            ) = 16
            AND cardinality(missing_fields) = 0)
        OR
        (analysis_status = 'insufficient_data'
            AND reaction_class IS NULL
            AND reaction_direction IS NULL
            AND reaction_timestamp IS NULL
            AND detection_delay_minutes IS NULL
            AND initial_return IS NULL
            AND max_favorable_excursion IS NULL
            AND max_adverse_excursion IS NULL
            AND max_retracement IS NULL
            AND analysis_status_reason IS NOT NULL
            AND cardinality(missing_fields) > 0)
    )
);

CREATE INDEX idx_earnings_reaction_features_date_status
    ON earnings_reaction_features (earnings_date, analysis_status);

CREATE INDEX idx_earnings_reaction_features_class
    ON earnings_reaction_features (reaction_class, earnings_date)
    WHERE reaction_class IS NOT NULL;

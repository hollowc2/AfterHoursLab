-- Distinguish scheduled evidence from later historical recovery. Existing raw
-- bars cannot be classified safely after the fact, while all existing coverage
-- rows were produced by the scheduled OHLCV capture workflow.
ALTER TABLE bar_evidence
    ADD COLUMN collection_mode TEXT NOT NULL DEFAULT 'legacy_unspecified'
    CHECK (collection_mode IN (
        'legacy_unspecified', 'manual_collection',
        'scheduled_capture', 'historical_backfill'
    ));

ALTER TABLE bar_evidence
    ALTER COLUMN collection_mode SET DEFAULT 'manual_collection';

ALTER TABLE earnings_ohlcv_coverage
    ADD COLUMN collection_mode TEXT NOT NULL DEFAULT 'scheduled_capture'
    CHECK (collection_mode IN ('scheduled_capture', 'historical_backfill'));

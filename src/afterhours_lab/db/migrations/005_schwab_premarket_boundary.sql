-- Schwab's standard premarket session begins at 07:00 ET (04:00 PT), not
-- 04:00 ET. Repair only the derived expectation metadata. Observed bars,
-- response hashes, retrieval timestamps, source, and raw bar evidence remain intact.
UPDATE earnings_ohlcv_coverage
SET expected_start = (market_date + TIME '07:00') AT TIME ZONE 'America/New_York',
    expected_minutes = (
        EXTRACT(EPOCH FROM (
            expected_end
            - ((market_date + TIME '07:00') AT TIME ZONE 'America/New_York')
        )) / 60
    )::INTEGER
WHERE phase = 'following_premarket';

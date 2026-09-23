-- Retroactive liquidity exclusion for earnings_events, mirroring the forward-only
-- $20M avg-dollar-volume floor archive-earnings applies to new candidates (see
-- archive_earnings.MIN_AVG_DOLLAR_VOLUME). Rows are flagged, never deleted, so the
-- raw event and every evidence table that references it stay intact; the research
-- layer excludes flagged rows from cohorts, quality issues, and distributions.
ALTER TABLE earnings_events ADD COLUMN liquidity_excluded_at TIMESTAMPTZ;
ALTER TABLE earnings_events ADD COLUMN liquidity_excluded_reason TEXT;

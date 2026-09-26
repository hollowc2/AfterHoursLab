-- Retire earnings_events rows the calendar has since moved. Finnhub re-dates a
-- fiscal quarter's print as the company firms it up, and because events are keyed on
-- (symbol, earnings_date), every move left the earlier date behind as a phantom event
-- (ANAB's 2026 Q2 was recorded on 2026-08-27, 2026-09-11, and 2026-09-21; only the
-- last is real). A superseded row is flagged, never deleted, so it and every evidence
-- row that references it stay queryable for audit. The research layer, the watchlist,
-- OHLCV capture, and the live monitor all skip it (see calendar_reconcile.py).
ALTER TABLE earnings_events ADD COLUMN superseded_at TIMESTAMPTZ;
ALTER TABLE earnings_events ADD COLUMN superseded_reason TEXT;
ALTER TABLE earnings_events ADD COLUMN superseded_by_date DATE;
ALTER TABLE earnings_events ADD CONSTRAINT earnings_events_supersession_check CHECK (
    (superseded_at IS NULL AND superseded_reason IS NULL AND superseded_by_date IS NULL)
    OR (
        superseded_at IS NOT NULL
        AND btrim(superseded_reason) <> ''
        AND superseded_by_date IS NOT NULL
        AND superseded_by_date <> earnings_date
    )
);

-- Phase 4 live-monitor evidence and durable operational health.
-- Quote evidence remains insert-only. These two quote fields were already part of
-- the typed gateway v1 contract but migration 003 did not have columns for them.
ALTER TABLE quote_evidence ADD COLUMN close DOUBLE PRECISION;
ALTER TABLE quote_evidence ADD COLUMN net_percent_change DOUBLE PRECISION;

CREATE TABLE monitor_cycles (
    id BIGSERIAL PRIMARY KEY,
    market_date DATE NOT NULL,
    cycle_started_at TIMESTAMPTZ NOT NULL,
    cycle_completed_at TIMESTAMPTZ NOT NULL,
    active_window BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'no_candidates', 'inactive', 'degraded')),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    fetched_quote_count INTEGER NOT NULL CHECK (fetched_quote_count >= 0),
    inserted_quote_count INTEGER NOT NULL CHECK (inserted_quote_count >= 0),
    conflicted_quote_count INTEGER NOT NULL CHECK (conflicted_quote_count >= 0),
    error_kind TEXT,
    error_message TEXT,
    CHECK (
        (status = 'degraded' AND error_kind IS NOT NULL AND error_message IS NOT NULL)
        OR (status <> 'degraded' AND error_kind IS NULL AND error_message IS NULL)
    )
);

CREATE INDEX idx_monitor_cycles_market_date_latest
    ON monitor_cycles (market_date, cycle_completed_at DESC, id DESC);

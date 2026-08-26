-- Lossless gateway evidence alongside the derived 1-minute candles. These tables
-- preserve only fields supplied by the gateway contract. The four nullable status
-- columns remain NULL (unknown) until an authoritative gateway field exists.
CREATE TABLE quote_evidence (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ,
    gateway_received_at TIMESTAMPTZ NOT NULL,
    session TEXT,
    capture_window TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    bid_size BIGINT,
    ask_size BIGINT,
    last DOUBLE PRECISION,
    last_size BIGINT,
    mark DOUBLE PRECISION,
    volume BIGINT,
    source TEXT NOT NULL,
    stale BOOLEAN NOT NULL,
    age_seconds DOUBLE PRECISION,
    data_quality_flags TEXT[] NOT NULL,
    schema_version TEXT NOT NULL,
    exto_eligible BOOLEAN,
    exchange_status TEXT,
    trading_status TEXT,
    session_eligible BOOLEAN,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, gateway_received_at, capture_window, earnings_date)
);

CREATE INDEX idx_quote_evidence_earnings_event
    ON quote_evidence (symbol, earnings_date, capture_window, gateway_received_at);

CREATE TABLE bar_evidence (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    session TEXT NOT NULL,
    evidence_date DATE NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT NOT NULL,
    gateway_event_timestamp TIMESTAMPTZ,
    gateway_received_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    gateway_endpoint TEXT NOT NULL,
    frequency TEXT NOT NULL,
    stale BOOLEAN NOT NULL,
    age_seconds DOUBLE PRECISION,
    data_quality_flags TEXT[] NOT NULL,
    schema_version TEXT NOT NULL,
    exto_eligible BOOLEAN,
    exchange_status TEXT,
    trading_status TEXT,
    session_eligible BOOLEAN,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, ts, session, gateway_endpoint, gateway_received_at)
);

CREATE INDEX idx_bar_evidence_symbol_date
    ON bar_evidence (symbol, evidence_date, session, ts);

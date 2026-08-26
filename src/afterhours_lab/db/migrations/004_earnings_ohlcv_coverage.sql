-- Auditable coverage for the four OHLCV phases surrounding an after-close report.
CREATE TABLE earnings_ohlcv_coverage (
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'earnings_regular', 'earnings_postmarket',
        'following_premarket', 'following_regular'
    )),
    market_date DATE NOT NULL,
    session TEXT NOT NULL CHECK (session IN ('regular', 'extended')),
    expected_start TIMESTAMPTZ NOT NULL,
    expected_end TIMESTAMPTZ NOT NULL,
    observed_first TIMESTAMPTZ,
    observed_last TIMESTAMPTZ,
    observed_minutes INTEGER NOT NULL CHECK (observed_minutes >= 0),
    expected_minutes INTEGER NOT NULL CHECK (expected_minutes > 0),
    source TEXT NOT NULL,
    gateway_received_at TIMESTAMPTZ NOT NULL,
    response_sha256 TEXT NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    data_quality_flags TEXT[] NOT NULL,
    calendar TEXT NOT NULL CHECK (calendar = 'XNYS'),
    calendar_version TEXT NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, earnings_date, phase),
    FOREIGN KEY (symbol, earnings_date)
        REFERENCES earnings_events (symbol, earnings_date)
);

-- Earnings events flagged by afterhours-lab-archive-earnings, and the three capture
-- windows recorded for each: the trading day before, the after-hours session that day,
-- and the trading day after.
CREATE TABLE earnings_events (
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    hour TEXT,
    day_before_captured BOOLEAN NOT NULL DEFAULT FALSE,
    after_hours_captured BOOLEAN NOT NULL DEFAULT FALSE,
    day_after_captured BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, earnings_date)
);

-- 1-minute candles for a flagged symbol, tagged with which capture window they belong
-- to (day_before / after_hours / day_after) and which earnings event they were
-- recorded for.
CREATE TABLE candles (
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    session TEXT NOT NULL,
    capture_window TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT,
    source TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts, session)
);

SELECT create_hypertable('candles', 'ts');

CREATE INDEX idx_candles_earnings_event
    ON candles (symbol, earnings_date, capture_window);

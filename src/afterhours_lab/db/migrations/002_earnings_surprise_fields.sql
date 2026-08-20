-- EPS/revenue estimate-vs-actual (the "surprise") is the biggest explanatory
-- variable for why a stock moved after a print. Finnhub's /calendar/earnings rows
-- already carry these fields; capture them instead of discarding them.
ALTER TABLE earnings_events
    ADD COLUMN eps_estimate DOUBLE PRECISION,
    ADD COLUMN eps_actual DOUBLE PRECISION,
    ADD COLUMN revenue_estimate DOUBLE PRECISION,
    ADD COLUMN revenue_actual DOUBLE PRECISION,
    ADD COLUMN quarter SMALLINT,
    ADD COLUMN year SMALLINT;

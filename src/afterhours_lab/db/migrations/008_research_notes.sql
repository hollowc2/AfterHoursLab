-- Discretionary researcher observations, kept strictly separate from computed
-- features so a note can never contaminate a reproducible calculation. Append-only
-- by application behavior: a correction is a new note, not an edit of an old one.
CREATE TABLE research_notes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    earnings_date DATE NOT NULL,
    author TEXT NOT NULL CHECK (btrim(author) <> ''),
    body TEXT NOT NULL CHECK (btrim(body) <> ''),
    tags TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (symbol, earnings_date)
        REFERENCES earnings_events (symbol, earnings_date)
);

CREATE INDEX idx_research_notes_event
    ON research_notes (symbol, earnings_date, created_at DESC);

CREATE INDEX idx_research_notes_tags
    ON research_notes USING GIN (tags);

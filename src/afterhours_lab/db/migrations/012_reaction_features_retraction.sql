-- Lets a stale earnings_reaction_features row be superseded without ever changing a
-- value a study may already have cited. No existing row is ever UPDATEd in place: a
-- stale row is retracted (a metadata-only stamp) and a fresh row is inserted
-- alongside it under the same version triple. Both rows stay in the table forever.
--
-- The natural key moves from a hard PRIMARY KEY to a partial UNIQUE index scoped to
-- live (non-retracted) rows, so a retraction + reinsert can coexist with the row it
-- superseded. A synthetic id becomes the new PRIMARY KEY since nothing else
-- references the natural key as a foreign key (checked: no other table has a FK into
-- earnings_reaction_features).
ALTER TABLE earnings_reaction_features DROP CONSTRAINT earnings_reaction_features_pkey;

ALTER TABLE earnings_reaction_features ADD COLUMN id BIGSERIAL PRIMARY KEY;
ALTER TABLE earnings_reaction_features ADD COLUMN retracted_at TIMESTAMPTZ;
ALTER TABLE earnings_reaction_features ADD COLUMN retracted_reason TEXT;
ALTER TABLE earnings_reaction_features ADD CHECK (
    (retracted_at IS NULL) = (retracted_reason IS NULL)
);

CREATE UNIQUE INDEX earnings_reaction_features_live_key
    ON earnings_reaction_features (
        symbol, earnings_date, feature_version, detector_version, classifier_version
    )
    WHERE retracted_at IS NULL;

-- Every existing read (research/datasets.py, research/outcomes.py) joins on the
-- natural key expecting at most one row per event; this index keeps that true for
-- callers that now add "retracted_at IS NULL" the same way they already do for
-- earnings_events.liquidity_excluded_at.

# AfterHoursLab Phase 5A — immutable studies and outcome closure implementation prompt

Use this prompt in a fresh coding session. Work autonomously through repository review,
implementation, deterministic tests, documentation, and a deployment-ready handoff. Do
not deploy to Helios, apply a live migration, install cron, or restart a live service until
the operator explicitly approves the completed release candidate and rollback plan.

## Objective

Build Phase 5A of AfterHoursLab: a reproducible, insert-only study registry that turns the
existing research notebook workflow into an auditable development/out-of-sample process,
plus versioned following-session outcomes derived only from authoritative captured evidence.

At completion, a researcher must be able to:

1. register an immutable study plan before evaluating it;
2. materialize and digest a complete development cohort with disclosed exclusions;
3. append a development result and freeze the exact rule produced by that work;
4. materialize the held-out cohort only after the rule is frozen;
5. apply that stored rule once to the held-out cohort without refitting it;
6. append an OOS result and a conservative decision;
7. reproduce every cohort, outcome, rule, and result from versioned database evidence.

This remains a research system. It must not place orders, construct broker orders, manage
positions, publish trading signals, or change any live-trading process.

## Starting point

Start from `main` at or after:

- `0056914` — Phase 4 live monitor, SSE, migration 009, and deployment configuration;
- `18ee53b` — bounded no-candidate cadence, true batched quote writes, and complete
  monitor/SSE lifecycle acceptance coverage.

The Phase 4 release deployed to Helios on 2026-08-30 has:

- Git SHA `18ee53b3472b09558179871ca668990b869fe777`;
- image `sha256:d0489fe66379a39d25edf14703be7266656ec8c8bdced4f2f00006e7636bbc1c`;
- migrations 001–009 applied;
- one `afterhours-lab-monitor` instance and one `afterhours-lab-web` instance;
- working `/healthz`, `/today`, `/quality`, and `/today/stream` behavior;
- a successful real gateway `$SPX` smoke through the candidate gateway;
- 330 passing tests and clean Ruff/Compose validation before deployment.

During Phase 4 deployment, Helios experienced approximately 90% hypervisor CPU steal. That
made valid gateway readiness endpoints miss their two-second request / three-second Docker
health deadlines. The operator has addressed the host issue and expects it to settle. At the
start and end of this phase, perform only a read-only health recheck; do not change or restart
SchwabGateway as part of Phase 5A.

Before editing, inspect the current repository, migration ledger conventions, research types,
notebook 04, deployed service state, and actual sample/coverage counts. Treat the figures above
as a handoff baseline, not a substitute for inspection.

## Phase boundary

Phase 5A includes:

- versioned following-session outcome computation and persistence;
- immutable study plans, staged development/OOS results, frozen rules, cohort membership,
  evidence digests, exclusions, and decisions;
- deterministic CLI workflows and stable machine-readable/report output;
- typed read access through `afterhours_lab.research`;
- an optional small read-only study detail/index view if it cleanly reuses the shared layer;
- deployment configuration for outcome persistence after authoritative following-session
  capture.

Explicitly defer:

- weekly scheduled cohort reports (Phase 5B);
- hypothetical alerting or a shadow decision journal (Phase 5C);
- expected/implied-move comparisons until an authoritative options-data contract exists
  (Phase 5D);
- strategy optimization, parameter sweeps, live alerts, paper orders, and real orders;
- any SchwabGateway health-check or infrastructure change.

Do not broaden Phase 5A merely because a future feature would be convenient.

## Existing architecture to reuse

- `afterhours_lab.research.EventFilter` is the typed description of a cohort.
- `afterhours_lab.research.fetch_cohort` is the shared feature/cohort read path and returns
  universe, included, excluded, and exclusion-status counts.
- `afterhours_lab.web.params.filter_to_query` round-trips web filters, but includes presentation
  state such as sorting and pagination. Extract or add a research-owned canonical serializer
  for study semantics rather than making persistence depend on a web module.
- `FeatureVersions` prevents mixed feature/detector/classifier generations.
- `earnings_reaction_features` is insert-only by application behavior and carries
  `source_evidence_sha256` plus the coverage identities used by the computation.
- `earnings_ohlcv_coverage` names the authoritative retrieval for each event phase.
- `bar_evidence` contains the raw bars for those retrievals. Outcome calculation must select
  bars through the named coverage identity; do not read whichever duplicate happens to be
  newest.
- `notebooks/04_out_of_sample_validation.ipynb` defines the intended discipline: state the
  hypothesis, inspect development only, freeze the exact rule, then open OOS once.
- `afterhours_lab.db.migrate` provides forward-only, checksummed migrations under an advisory
  lock.
- Existing persistence commands use dry-run support, bounded batches, advisory locks,
  idempotent inserts, and structured logging. Follow those conventions.

## Non-negotiable invariants

1. **Evidence is immutable.** Never update or overwrite raw market evidence, persisted
   reaction features, following-session outcomes, study plans, cohort snapshots, rules, or
   results. A correction is a new version/generation alongside the old one.
2. **One calculation engine per concept.** Following-session outcomes are calculated once in a
   dedicated versioned Python engine. SQL selects evidence; browser code, notebooks, templates,
   and report renderers do not calculate returns or classifications.
3. **No future leakage.** Same-day reaction features remain cut off at their existing label
   boundary. Following-session outcomes are separate labels and cannot become detector inputs.
4. **OOS stays sealed.** The application must prevent an OOS snapshot/result from being created
   before a development result has frozen a rule. OOS application uses that stored rule exactly;
   it must not recompute a median, threshold, sign, bucket, or parameter from OOS rows.
5. **Never mix generations.** A study plan pins the feature, detector, classifier, and outcome
   versions. Every member and result must match those versions.
6. **Missing evidence is data.** Incomplete or unavailable following-session evidence produces
   an explicit status, missing-field list, coverage disclosure, and quality flags. Do not fill,
   interpolate, synthesize, or silently exclude it.
7. **The complete universe is disclosed.** Freeze enough per-event membership information to
   reproduce both inclusion and exclusion. A stored study may not contain only the favorable
   rows that passed refinements.
8. **Digests are canonical.** All hashes use documented, stable serialization, deterministic
   ordering, explicit null representation, and versioned field sets. Database row order must not
   change a digest.
9. **Observational means observational.** Execution assumptions must explicitly say `none` for
   observational work. Do not translate outcome returns into fills, P&L, slippage, buying power,
   or trade recommendations.
10. **No trading authority.** The repository remains gateway market-data read-only and has no
    order scopes, brokerage credentials, or order client.
11. **Web remains a presentation client.** If study pages are added, they read through
    `afterhours_lab.research`; they contain no SQL, market calculations, or gateway client.
12. **Operational health is separate from research.** Monitor/gateway availability may be
    disclosed but never treated as an outcome or explanatory feature.

## Required implementation

### 1. Define a versioned following-session outcome contract

Add a pure, separately tested outcome engine and typed records for after-close earnings events.
Use a clearly named initial version such as `following-session-v1`; do not reuse the reaction
feature version.

The engine must read only the event's authoritative:

- `earnings_regular` coverage for the pre-event regular-session reference close;
- `following_premarket` coverage for the following-session premarket path;
- `following_regular` coverage for the following regular session.

Select raw bars by the exact coverage identity (symbol/event, phase/session/date, source,
gateway receipt/hash or the repository's established equivalent), not by an unconstrained
latest-row query. Preserve the coverage response hashes, collection modes, calendar/version,
and a digest of the exact source rows used.

For a complete row, compute a small, documented outcome set useful across many studies:

- reference regular-session close and timestamp;
- following premarket first, high, low, and last prices and returns versus reference;
- following regular open, high, low, and close prices and returns versus reference;
- fixed following-regular returns at 5, 30, and 60 elapsed session minutes when those exact
  close-confirmed bars exist;
- overnight gap/open return and full following-session close return;
- observed/expected bar counts and coverage ratios for each required phase.

Use exchange-calendar session boundaries and existing bar timestamp semantics. Define precisely
whether a horizon means the close of minute N and test it. Do not substitute nearest bars when an
exact required bar is absent. Do not derive trade entry/exit prices.

Support at least these statuses:

- `complete` — all required reference and following-session values exist;
- `insufficient_data` — required evidence or an exact horizon is missing;
- `not_yet_available` — the following exchange session or its authoritative capture is not yet
  complete.

Keep `not_yet_available` distinct from bad coverage. Re-running after evidence arrives may insert
the completed generation only if the persistence key/version contract makes the transition
append-only and unambiguous. Prefer not persisting transient `not_yet_available` rows unless the
schema explicitly models attempts separately from final outcomes.

Add a forward-only migration, expected to be `010_phase5_studies.sql`, with a versioned,
insert-only outcome table. It must include:

- event identity and outcome version;
- analysis status/reason;
- source coverage identities and collection modes;
- source-evidence digest;
- explicit parameters/cutoff metadata;
- nullable outcome values guarded by status-sensitive checks;
- missing fields and data-quality flags;
- computation timestamp;
- a natural key that allows new versions without overwriting old results.

### 2. Add outcome persistence and scheduling

Add a CLI such as:

```bash
uv run afterhours-lab-persist-outcomes --from 2026-08-01 --to 2026-08-31 --dry-run
uv run afterhours-lab-persist-outcomes --from 2026-08-01 --to 2026-08-31
```

Requirements:

- validate dates and bound the requested range;
- use one non-blocking advisory lock for the batch;
- read through typed research/evidence helpers rather than embedding duplicated universe SQL;
- calculate pure rows before persistence;
- insert in bounded batches with `ON CONFLICT DO NOTHING`;
- report scanned, complete, insufficient, not-yet-available, inserted, and conflicted counts;
- make exact reruns idempotent;
- fail terminally on schema/contract errors and disclose transient database errors;
- support a deterministic clock for tests;
- never call SchwabGateway: outcome closure consumes already captured database evidence.

Add a DST-safe wrapper and cron snippet for a conservative time after the authoritative 4:05 PM
ET `following_regular` capture, recommended 4:25 PM ET on exchange weekdays. Re-scan a documented
trailing range long enough to close Friday/holiday events and late captures. This job must remain
independent of the same-day 8:25 PM reaction-feature job.

Do not install the cron entry during implementation. Include it in the later Helios approval gate.

### 3. Add an immutable staged study registry

Model the notebook-04 lifecycle without mutable state. Use normalized tables or an equally strict
schema that captures these concepts:

#### `study_versions`

An immutable pre-registered plan keyed by a stable study slug/key and positive version. Store at
least:

- title and one-sentence hypothesis;
- author/owner;
- created timestamp;
- canonical universe/refinement filter, excluding pagination and display sorting;
- pinned feature/detector/classifier/outcome versions;
- explicit development and OOS date ranges that do not overlap;
- primary metric and expected direction;
- planned analysis description;
- minimum development/OOS sample requirements;
- execution assumptions (`none` for observational studies);
- known limitations/failure modes;
- canonical specification JSON and SHA-256 digest.

Registration must not query or disclose OOS outcomes.

#### Staged `study_results`

Append-only result rows for `development` and `oos` stages. Each row stores:

- its study-version identity and result generation;
- stage and timestamps;
- cohort/evidence snapshot digest;
- universe, included, excluded, complete-outcome, and insufficient counts;
- exclusion breakdown in a deterministic typed shape;
- metric name, result values, uncertainty/sample statistics actually used, and units;
- for development: the exact frozen rule/thresholds and a rule digest;
- for OOS: a reference to the development result/rule digest used unchanged;
- assumptions, limitations, quality disclosures, and failure modes;
- a conservative decision: `reject`, `refine`, or `paper_trade_candidate`;
- canonical result JSON and digest.

The schema/application must reject:

- OOS insertion without an existing development result and frozen rule;
- an OOS result whose rule digest differs from the referenced development rule;
- version mismatches between study, cohort members, outcomes, and features;
- overlapping development/OOS periods;
- empty hypotheses, rules, assumptions, or decision rationale;
- `paper_trade_candidate` when declared minimum sample or required coverage gates are not met.

#### Frozen cohort membership

Persist deterministic member rows associated with each staged result. Include the complete scoped
universe, not only included rows. Each member must record:

- symbol and earnings date;
- stage;
- included/excluded status and stable reason code(s);
- pinned feature identity/version and feature evidence digest when available;
- pinned following-outcome identity/version and outcome evidence digest when available;
- any quality/status fields needed to explain exclusion;
- a canonical per-member digest.

The result's cohort digest must hash the ordered member records, their inclusion decisions, and
their evidence identities. A change in source evidence, membership, exclusion reason, version, or
outcome must change the digest.

Do not implement the lifecycle by updating a `status` column. Registration, development result,
and OOS result are distinct immutable insert events.

### 4. Add a safe study CLI workflow

Use a JSON study-spec file with strict typed validation and canonical serialization. Do not add a
YAML dependency solely for this phase.

Provide a coherent CLI, for example:

```bash
# validate and show canonical plan/digest; no database write
uv run afterhours-lab-study register --spec studies/example.json --dry-run

# insert immutable plan only
uv run afterhours-lab-study register --spec studies/example.json

# materialize development, calculate the declared analysis, and append its frozen rule/result
uv run afterhours-lab-study evaluate-development --study delay-retention --version 1 --dry-run
uv run afterhours-lab-study evaluate-development --study delay-retention --version 1

# only after development exists; apply its stored rule once without refitting
uv run afterhours-lab-study evaluate-oos --study delay-retention --version 1 --dry-run
uv run afterhours-lab-study evaluate-oos --study delay-retention --version 1

# deterministic human and machine-readable inspection
uv run afterhours-lab-study show --study delay-retention --version 1
uv run afterhours-lab-study show --study delay-retention --version 1 --format json
```

Exact command names may improve during implementation, but preserve the staged safety properties.

Requirements:

- every write command supports `--dry-run`;
- dry-run executes validation/calculation but performs no inserts;
- write commands use a study-specific non-blocking advisory lock and one transaction for each
  immutable event plus its member rows;
- exact reruns are idempotent, while the same key/version with different canonical content fails
  loudly rather than silently returning the old row;
- no command accepts arbitrary SQL, Python expressions, or executable rule text;
- rules are a typed, whitelisted data structure interpreted by tested Python code;
- report output discloses counts, exclusions, pinned versions, digests, missing evidence, and
  whether sample gates passed;
- JSON output is stable enough for later Phase 5B report automation.

Implement one useful initial study-analysis contract matching notebook 04 (for example, a frozen
development-derived detection-delay split evaluated against retention), but design a small typed
registry so later analyses add explicit implementations rather than arbitrary expressions. Do not
build a general backtesting DSL.

### 5. Put all reads behind the shared research layer

Add typed research records/functions for:

- following-session outcomes;
- study versions;
- staged results and member/exclusion summaries;
- one complete study detail suitable for CLI/report/web rendering.

Study and outcome modules may own narrowly scoped insert SQL. Cohort, evidence, and report reads
belong in `afterhours_lab.research`. Do not put SQL in notebooks, templates, CLI formatting code,
or route handlers.

If a web view is added, keep it read-only and server-rendered. A normal page load must disclose
the hypothesis, periods, pinned versions, sample sizes, exclusions, frozen rule, OOS result,
decision, limitations, and digests. JavaScript may not calculate study metrics.

### 6. Update notebook 04 and documentation

Refactor notebook 04 into a client/example of the implemented study workflow:

- no SQL;
- no duplicated calculation engine;
- no database write through a read-only notebook pool;
- outputs cleared before commit;
- demonstrate plan validation and reading an already persisted study, or generate a JSON spec for
  the CLI rather than bypassing it;
- preserve the pedagogical order: hypothesis → development → freeze → OOS → decision.

Update README documentation with:

- the evidence → reaction feature → following outcome → study flow;
- exact outcome definitions and horizon semantics;
- immutable study lifecycle and OOS seal;
- CLI dry-run/write/show examples;
- cron timing and why outcome closure is separate from reaction persistence;
- missing-data and exclusion semantics;
- sample-size limitations and conservative decision language;
- migration, deployment, rollback, and operational commands.

Commit at least one non-promotional example study specification that validates but is not claimed
to have a favorable result. Do not commit secrets, generated reports with sensitive paths, large
exports, or notebook outputs.

### 7. Preserve deployment behavior and address build evidence proportionately

Phase 4's image build was pathologically slow while Helios had 90% CPU steal. First recheck build
behavior under normal CPU allocation. Only if the recursive ownership layer remains materially
slow, make a narrow Dockerfile improvement that preserves:

- runtime UID/GID 1001;
- writable `/app/data` bind-mount expectations;
- read-only root filesystems and `/tmp` tmpfs for persistent services;
- the existing locked `uv` environment and command paths.

Do not turn Phase 5A into a Docker refactor, and do not claim the Dockerfile caused the measured
delay without evidence.

## Canonicalization requirements

Document and centralize canonical serialization. At minimum:

- UTF-8 JSON;
- sorted object keys;
- compact separators;
- ISO-8601 dates/timestamps with explicit timezone where applicable;
- ordered/deduplicated enum and symbol lists;
- normalized finite numeric representation; reject NaN and infinities;
- explicit nulls where field presence is semantically important;
- deterministic event ordering by `(earnings_date, symbol)` or a documented equivalent;
- a schema/version name included in every digest input.

Never hash `str(dict)`, database driver record representations, unordered sets, rendered HTML, or
human-formatted reports.

## Testing requirements

Use `uv`, pytest async mode, repository fixtures/fakes, and Ruff line length 100. Do not run
`ruff format` across the repository.

Add deterministic direct tests covering at minimum:

### Outcome engine and persistence

- EST/EDT and exchange-session boundaries, weekends, and holidays;
- exact minute-horizon semantics;
- authoritative coverage selection when duplicate raw retrievals exist;
- complete, insufficient, and not-yet-available outcomes;
- no nearest-bar substitution and no synthetic values;
- exact source identities, collection modes, flags, missing fields, and digest inputs;
- outcome digest stability under input reordering and sensitivity to any evidence change;
- versioned insert idempotency and conflict counts;
- dry-run no-write behavior;
- duplicate advisory-lock behavior and resource cleanup;
- full CLI lifecycle with a fake database.

### Study registry and OOS seal

- strict JSON-spec validation and rejection of unknown fields;
- stable canonical spec/rule/member/result digests;
- semantic filter canonicalization excludes sort, page, offset, and display limit;
- exact duplicate registration is idempotent;
- same key/version with changed content fails;
- development and OOS ranges cannot overlap and must fit the declared universe;
- complete-universe membership includes excluded events with stable reasons;
- member/cohort digest changes when evidence, inclusion, exclusion, or versions change;
- OOS snapshot/result is impossible before a frozen development rule exists;
- OOS evaluation uses the stored rule and never recomputes its threshold from OOS;
- mismatched feature/outcome/rule versions fail;
- insufficient OOS coverage/sample size is disclosed and blocks `paper_trade_candidate`;
- append-only transaction rollback leaves neither orphan results nor member rows;
- dry-run performs no writes;
- human/JSON reports contain all required disclosures without recalculation.

### Regression and architecture

- the web process still cannot construct a gateway client;
- outcome persistence makes zero gateway calls;
- notebooks remain read-only clients;
- no SQL appears in routes, templates, notebook cells, or browser JavaScript;
- existing monitor, SSE, research, persistence, migration, and website tests remain green;
- migration discovery/checksum tests include migration 010.

Run and report exactly:

```bash
uv run pytest -q
uv run ruff check .
git diff --check
docker compose config --quiet
```

Also run local fake-backed smoke commands for plan registration, development evaluation, OOS
gating, outcome persistence, and report rendering. No test may depend on live market hours,
Helios, the live database, or the live gateway.

## Deployment and operations gate

At implementation completion, stop before all live changes and present a fresh Helios gate.

The read-only preflight must state:

- current `billy@helios:/opt/afterhours-lab` branch/SHA and intended SHA;
- current and intended image IDs/tags;
- running web/monitor uniqueness, state, restart counts, and loopback exposure;
- migration ledger state and intended migration 010;
- gateway `/health` and `/ready` behavior plus Docker health state after CPU steal recovery;
- host CPU steal/load, disk space, and recent filtered application errors;
- current cron entries relevant to capture, reaction persistence, and proposed outcome closure;
- exact database-write and gateway impact.

Expected impact must say explicitly:

- migration 010 is additive and forward-only;
- outcome persistence reads PostgreSQL evidence and writes versioned derived outcomes only;
- study commands write only when manually invoked;
- the new scheduled outcome job makes zero gateway calls;
- web and monitor behavior/ports remain unchanged unless the implementation truly requires a
  targeted web rebuild;
- no order, balance, position, or live-trading path exists.

After explicit approval, validate in this order:

1. preserve/tag the current image and record the rollback SHA;
2. update the checkout to the exact approved SHA;
3. build the image without interrupting current services;
4. apply migration 010 and verify the ledger/schema;
5. run outcome dry-run and a bounded persistence smoke on known completed evidence;
6. run study registration/development dry-runs and prove OOS gating;
7. rebuild/restart only services actually changed;
8. install the outcome cron additively only if separately included in the approval;
9. verify service state/logs/health, cron uniqueness, database rows/digests, and no gateway calls;
10. retain the rollback image until the release is accepted.

Rollback is service-first and evidence-preserving:

- disable/remove only the newly installed outcome-cron line;
- restore the previous approved Git SHA and image for affected services;
- leave additive migration 010 and any valid insert-only rows in place;
- never delete study/outcome rows or edit the migration ledger to simulate rollback;
- if a schema correction is needed, create a new forward migration;
- verify web/monitor health and existing nightly jobs after restoration.

## Completion handoff

Provide:

1. concise outcome first;
2. files and behavior changed;
3. exact outcome definitions and version names;
4. migration tables, keys, checks, and append-only guarantees;
5. study lifecycle, OOS-seal enforcement, and canonical digest rules;
6. test commands and exact results;
7. sample-size and data-quality findings from read-only inspection, without overstating them;
8. invariants checked, especially zero gateway calls from outcome persistence and zero order
   authority;
9. the complete Helios approval gate and rollback procedure;
10. clearly separated remaining work for Phases 5B, 5C, and 5D.

## Acceptance criteria

Phase 5A is complete only when:

- following-session outcomes are versioned, reproducible, insert-only, and derived from exact
  authoritative evidence;
- missing/not-yet-available evidence is explicit and never synthesized;
- an immutable study plan pins all filter, feature, outcome, split, and analysis semantics;
- the complete development universe and exclusions are frozen and digestible;
- a development result freezes a typed rule before OOS can be materialized;
- OOS applies the stored rule exactly once without refitting;
- study results and member rows are atomic, append-only, version-consistent, and reproducible;
- conservative sample/coverage gates prevent unsupported promotion language;
- all database reads are available through the shared typed research layer;
- CLI/report/web clients do not duplicate calculations or SQL;
- outcome persistence and study evaluation make no gateway calls and have no trading authority;
- full tests, Ruff, diff checks, and Compose validation pass;
- deployment remains behind a new explicit Helios approval gate.

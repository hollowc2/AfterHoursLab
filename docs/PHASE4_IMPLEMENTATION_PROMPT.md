# AfterHoursLab Phase 4 — live monitor and SSE implementation prompt

Use this prompt in a fresh coding session. Work autonomously through implementation,
tests, documentation, and a deployment-ready handoff. Do not deploy to Helios until the
operator explicitly approves the completed release candidate and rollback plan.

## Objective

Build Phase 4 of AfterHoursLab: an always-on, read-only-market-data monitoring daemon
that persists raw live quote evidence for today's after-close earnings candidates, and
Server-Sent Events (SSE) that refresh `/today` from the database without reimplementing
research calculations in JavaScript.

The finished system must remain useful when the gateway, monitor, or SSE connection is
degraded. A failure must be disclosed as stale or missing evidence; it must never be
presented as a market observation.

## Starting point

Start from `main` after these commits have landed:

- `55f701f` — shared research-data layer, feature persistence, and research website
- `b1bca1f` — read-only research notebook starter kit
- `16289d9` — nightly feature-persistence cron and always-on web Compose service

The deployed Phase 0–3 baseline has been validated against the live Helios database:

- migrations 007 and 008 are applied;
- 49 versioned feature rows were backfilled (14 `complete`, 35 `insufficient_data`);
- the web service runs on `127.0.0.1:8055` with `restart: unless-stopped`;
- the nightly reaction persistence job is installed for 8:25 PM ET;
- 307 tests pass and Ruff is clean.

Before editing, inspect the current repository and confirm this context rather than
assuming line numbers or APIs remain unchanged.

## Existing architecture to reuse

- `afterhours_lab.gateway.BoundedGatewayClient` wraps the official typed
  SchwabGateway SDK and supplies bounded concurrency and retry behavior.
- `quote_evidence` (migration 003) is the lossless quote-evidence table. Its unique key
  is `(symbol, gateway_received_at, capture_window, earnings_date)`.
- `bar_evidence` stores raw candle evidence. Do not manufacture minute bars from quote
  polls.
- `afterhours_lab.research.fetch_today` is the single read path for `/today`. It already
  joins today's archived AMC events, latest quote evidence, pre-close reference,
  provisional postmarket bars, and persisted final features.
- `afterhours_lab.web` is FastAPI + Jinja + vendored browser assets. It currently owns
  no polling or market-data client.
- `afterhours-lab-web` is an always-on Compose service. Batch capture and feature
  persistence are separate processes.

## Non-negotiable invariants

1. **One calculation engine.** Do not calculate reaction features, returns, thresholds,
   classifications, spread percentages, or freshness policy in browser JavaScript or in
   the monitor. Raw evidence is written by the monitor; presentation data comes through
   `afterhours_lab.research`.
2. **Insert-only evidence and features.** Never update or overwrite a persisted market
   observation or feature generation. Duplicate evidence may be ignored only through a
   documented natural/idempotency key.
3. **Never mix feature generations.** Existing `EventFilter.versions` behavior remains
   intact.
4. **Provisional is not finalized.** Live values and SSE updates remain visibly marked
   provisional until a persisted feature row exists.
5. **Missing evidence is explicit.** Never fill, interpolate, synthesize, or infer a
   missing quote or minute.
6. **Operations are not research.** Monitor health belongs in operational freshness and
   degradation reporting, not reaction statistics.
7. **The web process does not call SchwabGateway.** It only reads PostgreSQL. Capture
   continues if the website is down, and website restarts do not cause gateway traffic.
8. **Gateway-only market data.** This repository holds no Schwab credentials and uses
   only the internal gateway with the existing `market_data:read` application identity.

## Required implementation

### 1. Add `afterhours-lab-monitor`

Create a new module and project CLI entry point for an always-on daemon.

Behavior:

- Determine the current `America/New_York` market date on every scheduling cycle; do
  not cache a date across midnight.
- Query the database for that date's archived `hour = 'amc'` candidates through a
  small typed dataset function in `afterhours_lab.research`. Do not copy the earnings
  universe SQL into the daemon.
- During a clearly named and documented active window, poll the gateway's batched quote
  endpoint at a configurable interval. Recommended defaults are 5 seconds and
  15:50–20:15 ET on exchange weekdays. Keep the window and interval explicit settings
  so deployment can tune them without a code change.
- Outside the active window, sleep until the next relevant boundary without issuing
  gateway requests. Cap any sleep so clock/date changes are noticed.
- Persist one `quote_evidence` row per quote returned by the gateway, preserving only
  fields supplied by the typed gateway contract. Use a distinct, stable
  `capture_window` such as `live_reaction_monitor`.
- Preserve gateway timestamps, stale state, age, quality flags, schema version, source,
  and all available quote fields exactly. Unknown status fields remain `NULL`.
- Do not create candle rows from quotes. Existing authoritative OHLCV capture remains
  responsible for `bar_evidence` and coverage rows.
- If no candidates exist, issue no gateway call and log one bounded, structured status
  message rather than spinning.
- Use a non-blocking Postgres advisory lock for process uniqueness. A duplicate instance
  must log and exit cleanly before making gateway requests.
- Batch inserts in a transaction and make retries/idempotency safe with
  `ON CONFLICT DO NOTHING`. Report fetched, inserted, and conflicted counts.
- Gateway transient failures use the existing bounded retry client, then back off with a
  cap. Authentication/authorization/contract failures are not silently retried forever.
- Support graceful SIGTERM/SIGINT shutdown: stop scheduling, finish or cancel the current
  bounded operation safely, close gateway and database clients, and exit zero.
- Add `--once`, `--date`, and configurable interval/window arguments or settings so the
  complete path can be tested deterministically without waiting for market hours.
- The production service must not use `watch.py`; that is an interactive terminal viewer
  and does not persist evidence.

Keep row construction pure and separately tested. Put SQL that reads research tables in
`afterhours_lab.research`; put the narrowly scoped evidence insert in the monitor module
or a clearly named persistence module. Do not put monitor-specific SQL in web routes.

### 2. Make monitor health observable

Prefer deriving operational health from evidence the daemon actually writes. If a
separate heartbeat/run table is necessary to distinguish “no candidates” from “monitor
never ran,” add one forward-only migration with a narrow operational schema. Do not
write heartbeat rows into research feature tables.

Expose enough typed state through `afterhours_lab.research` for `/today` and `/quality`
to disclose at least:

- last successful monitor cycle;
- last gateway failure or degraded cycle, without secrets;
- active/inactive state for the requested market date;
- number of monitored candidates and last inserted quote count;
- age of the freshest quote evidence.

Do not invent an in-memory health source that disappears when the daemon restarts.

### 3. Add `/today/stream` SSE

Add a standards-compliant SSE endpoint to the existing FastAPI app.

- Accept the same optional ISO `date` parameter as `/today`.
- The endpoint reads through `afterhours_lab.research`; it never calls the gateway and
  contains no feature calculation.
- Send an initial event immediately, then send an update only when the relevant database
  state changes. A short database polling loop inside each SSE connection is acceptable
  at current scale; make interval, disconnect handling, and cleanup explicit.
- Emit periodic SSE comment/keepalive frames so proxies do not treat an idle healthy
  stream as dead.
- Use deterministic event IDs or a state digest so duplicate unchanged payloads are not
  emitted. Honor disconnects promptly and leak no tasks or database acquisitions.
- Bound resource use: do not hold a database connection while sleeping, define a sane
  maximum update cadence, and document expected per-client cost.
- Set appropriate SSE headers (`text/event-stream`, no buffering/cache as applicable).
- A temporary database failure should produce a disclosed degraded event or reconnectable
  stream failure; it must not fabricate empty market data.

Prefer server-rendered HTML fragments in SSE payloads (or a server-built typed payload)
that replace the candidate table and freshness/degradation panels. JavaScript may apply
the received fragment and show connection state, but may not derive prices, returns,
threshold crossings, classes, or health policy.

### 4. Update `/today`

- Add the smallest vendored/local JavaScript needed to open `EventSource` for the
  currently selected date and apply server-produced updates.
- Show connected, reconnecting, stale, and unavailable states accessibly.
- Preserve full functionality without JavaScript: a normal page load still renders the
  complete current snapshot.
- Historical `?date=` pages must remain useful. Either stream their immutable state once
  and close or disable live reconnect behavior with a clear label.
- Keep all provisional/finalized and degradation disclosures already present.
- Do not add external assets or CDNs.

### 5. Package and deploy configuration

- Add `afterhours-lab-monitor` to `[project.scripts]`.
- Add a separate `afterhours-lab-monitor` Compose service using the same image, `.env`,
  `monitoring_net`, read-only filesystem, `/tmp` tmpfs, dropped capabilities, and
  `no-new-privileges` posture as the web service.
- Use `restart: unless-stopped` for the monitor. Do not schedule the daemon with cron;
  its own Eastern-time gate controls when it polls.
- Ensure the monitor and web services can be rebuilt independently and that batch cron
  commands continue to use the existing `afterhours-lab` service.
- Do not expose a new host port for the monitor.
- Document start, stop, logs, one-shot smoke, and rollback commands.
- Preserve the nightly 8:05 PM OHLCV capture and 8:25 PM feature persistence ordering.
  The monitor does not replace either job.

## Testing requirements

Use the repository conventions: `uv`, pytest async mode, Ruff line length 100, and do
not run `ruff format` across the repository.

Add deterministic tests covering at minimum:

- active-window boundaries in EST and EDT, weekdays, weekends, midnight/date rollover,
  and a non-session/holiday if the exchange calendar is used;
- no-candidate behavior and batched symbol selection;
- exact quote-to-row mapping, nullable gateway fields, stale/quality metadata, and no
  synthetic values;
- insert idempotency and conflict counts;
- advisory-lock duplicate-instance behavior;
- transient gateway failure/backoff and terminal auth/contract failure behavior;
- `--once` operation and graceful shutdown/resource cleanup;
- monitor health dataset mapping;
- SSE content type, initial event, update deduplication, keepalive, date validation,
  disconnect cleanup, and database-error behavior;
- `/today` progressive enhancement and historical-date behavior;
- proof that the web app never constructs a gateway client;
- existing research, persistence, website, and notebook tests remain green.

Use fakes and an injectable clock/sleeper for unit tests. Do not make tests depend on
wall-clock market hours, the live database, Helios, or the live gateway.

Run and report:

```bash
uv run pytest -q
uv run ruff check .
docker compose config
```

Also perform a local one-shot smoke with fake/test infrastructure if the repository can
support it safely.

## Documentation and handoff

Update the README with:

- the monitor's data flow and the fact that it writes raw evidence only;
- active window and polling defaults;
- SSE behavior and no-JavaScript fallback;
- operational/degradation semantics;
- Compose lifecycle and log commands;
- how nightly finalization remains separate;
- a concise resource/capacity note for gateway polling and SSE clients.

At completion, provide:

1. files and behavior changed;
2. tests and exact results;
3. migration details, if any;
4. invariants checked;
5. a Helios preflight and explicit approval gate containing current SHA/image, intended
   SHA/image, targeted services, expected gateway/database impact, validation steps, and
   exact rollback procedure;
6. what remains for Phase 5.

Do not deploy, migrate the live database, rebuild/restart a live Helios service, alter
Nginx, or expose a port without explicit operator approval after presenting that gate.

## Acceptance criteria

Phase 4 is complete only when:

- exactly one monitor instance can persist lossless quote evidence for today's AMC
  candidates during the configured window;
- gateway or database degradation is visible and never converted into a market value;
- `/today` updates through SSE without reload while retaining a complete no-JavaScript
  fallback;
- no calculation has moved into JavaScript, SQL, or the daemon;
- the web service still makes zero gateway calls;
- finalized feature persistence remains versioned, insert-only, and separate;
- the full test and lint suites pass;
- Compose configuration is deployment-ready and the live deployment remains behind an
  explicit approval gate.

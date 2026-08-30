# AfterHoursLab

A read-only market-data lab application. It has no Schwab credentials of its own and
never will — all market data comes exclusively from the standalone internal gateway at
[hollowc2/SchwabGateway](https://github.com/hollowc2/SchwabGateway) over its HTTP API
(`/v1/quotes`, `/v1/history`, and `/v1/session-history` for evidence collection;
`/v1/spot` for health smoke checks), authenticated with a pre-issued internal API key.

This app is registered in the gateway's auth model as:

- application id: `afterhours-lab`
- capability: `market_data:read`
- priority class: `background`

There is no order, account, position, or streaming code here, and none will be added —
those routes don't exist on the gateway and are out of scope for this app.

## Setup

```bash
uv sync
```

Copy `.env.example` to `.env` and fill in `SCHWAB_GATEWAY_URL` and
`SCHWAB_GATEWAY_API_KEY` with values for a real, already-issued gateway key. This app
does not and cannot issue its own key — key issuance is an explicitly-approved operator
action on the SchwabGateway side:

```bash
schwab-gateway-issue-keys --application-id afterhours-lab --capability market_data:read --priority background
```

That command runs against the SchwabGateway repo, not this one, and is out of scope for
this repo's scaffolding.

## Development

```bash
uv run pytest
uv run ruff check .
```

## Smoke test

Once `SCHWAB_GATEWAY_URL` and `SCHWAB_GATEWAY_API_KEY` are set in the environment:

```bash
uv run afterhours-lab-smoke
```

This fetches a spot quote for `$SPX` through the gateway and prints the validated
response. It exits non-zero and logs a clear message on any gateway client error
(authentication, authorization, timeout, capacity, or unavailability).

## Watchlist viewer

```bash
# manage a persisted watchlist (defaults to ./watchlist.json)
uv run afterhours-lab-watch --add SPY
uv run afterhours-lab-watch --add QQQ
uv run afterhours-lab-watch --remove QQQ

# watch the persisted list, polling every 5s by default
uv run afterhours-lab-watch

# or watch an ad-hoc list for just this run, without touching the persisted file
uv run afterhours-lab-watch SPY QQQ '$SPX' --interval 10
```

Renders a live-refreshing table (last/bid/ask/mark/volume/staleness/data-quality
flags) via `/v1/quotes`. Backs off for `BACKOFF_SECONDS` on gateway capacity or
availability errors rather than retrying immediately, since this app is registered as
`priority: background`.

The shared client enforces an explicit request timeout, bounds concurrent gateway
attempts, and retries only transient timeout/capacity/upstream failures with exponential
backoff. Authentication (401), authorization (403), and malformed versioned responses
fail closed without retrying. Defaults can be tuned with
`SCHWAB_GATEWAY_TIMEOUT_SECONDS`, `SCHWAB_GATEWAY_MAX_CONCURRENCY`,
`SCHWAB_GATEWAY_MAX_ATTEMPTS`, and `SCHWAB_GATEWAY_RETRY_BACKOFF_SECONDS`.

## Earnings calendar archiving

The gateway has no earnings-calendar data (out of scope for its contract), so this
uses [Finnhub](https://finnhub.io)'s free `/calendar/earnings` endpoint instead — an
independent source with its own API key, unrelated to the SchwabGateway connection.
Set `FINNHUB_API_KEY` in `.env`.

Base behavior: find symbols reporting earnings **after market close**, record each as
a row in `earnings_events` (idempotent — a symbol/date already on file is left alone),
then rewrite the persisted watchlist to exactly the symbols currently within their
capture window (the trading day before their earnings date, through the trading day
after), so the watch viewer only ever shows what's actually still relevant. The
watchlist file is a materialized view of `earnings_events`, not an accumulating log —
each run replaces its contents rather than merging into them, and a symbol drops out
on its own once its window passes, even on a run that finds no new earnings.

```bash
# the active window's after-close earnings: recorded to earnings_events,
# watchlist.json refreshed. Defaults to previous..next trading day (see below).
uv run afterhours-lab-archive-earnings

# a date range, without writing to earnings_events or the watchlist
uv run afterhours-lab-archive-earnings --from 2026-08-19 --to 2026-08-21 --dry-run
```

Requires both `FINNHUB_API_KEY` and the `DATABASE__*` settings below — `--dry-run` is
the only mode that doesn't touch either the database or the watchlist file.

**Default date range.** With no `--from`/`--to`, a run covers the previous through the
next trading day, matching the capture window itself. Both ends are load-bearing: the
3:55 PM `day_before` capture works on symbols whose earnings date is *tomorrow*, so
tomorrow's after-close names have to be in `earnings_events` by the morning run; and
refetching yesterday backfills `eps_actual`/`revenue_actual` for prints that hadn't
reported yet when they were first recorded.

**Watchlist file format.** `watchlist.json` carries the date it was written for:

```json
{ "as_of": "2026-08-21", "symbols": ["AAA", "BBB"] }
```

Without `as_of` a stale watchlist is indistinguishable from a current one — which is
how a list of symbols from two days earlier ends up on screen looking authoritative.
`afterhours-lab-watch` checks it and prints a warning (it still runs; it's an
interactive viewer) when the file predates the most recent trading day. A Friday file
read over the weekend is correctly treated as current, since no run is scheduled in
between. A bare JSON array is still accepted on read — the pre-`as_of` format, and a
convenient thing to hand-write — and always counts as stale.

A quiet day with no after-close names writes an *empty* watchlist stamped with today's
date, rather than keeping the previous day's symbols. That's a real answer, not a
failure: nothing is in the capture window.

This is the base data source for the lab; other earnings-driven studies can build on
top of it later.

**Re-entrancy guard.** If a run is still in progress when the next cron fire starts
(e.g. stuck in gateway backoff, since this app is `priority: background`), a second
process racing the first on `_upsert_earnings_events` / `save_watchlist` could corrupt
either. Every non-dry-run invocation takes a non-blocking Postgres advisory lock
(`ARCHIVE_LOCK_KEY` in `archive_earnings.py`, a fixed key distinct from
`db/migrate.py`'s `ADVISORY_LOCK_KEY`) before touching `earnings_events` or the
watchlist file. If another run already holds it, this run logs, records a (non-failure)
skip in `last_run_status.json`, and exits `0` rather than blocking — a wedged process
shouldn't cause its replacements to queue up behind it too.

**Observability.** A run's outcome is recorded in `last_run_status.json`, written next
to the watchlist file (`ok`/`detail`/`at`), so success or failure is visible without
grepping `archive_earnings.log`. Any failure — the earnings-calendar fetch, the DB
write, or the watchlist write (the exact failure mode from the 2026-08-20 incident,
where a `PermissionError` on the watchlist write crashed silently with only the cron
log to show for it) — also fires an optional Telegram alert via `notify.send()`, the
same lightweight pattern Butterflyguy's `schwab_token_keepalive.py` cron job uses (see
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env.example`). Both env vars are
optional: unset, `notify.send()` silently no-ops and `last_run_status.json` is the only
signal, which is why it's written unconditionally rather than being an add-on to the
Telegram path. This deliberately skips a healthchecks.io/cronitor-style external ping —
no new external service, reuses infra the operator already runs for Butterflyguy.

## Database

AfterHoursLab records 1-minute candles for flagged earnings symbols in its own
isolated database on the shared TimescaleDB instance (same per-app-database convention
Butterflyguy uses — never shares credentials or a database with another app). Schema is
raw SQL migrations under `src/afterhours_lab/db/migrations/`, tracked in a
`schema_migrations` ledger with a Postgres advisory lock guarding concurrent runs.

Core tables: `earnings_events` (one row per flagged symbol/date — populated by
`afterhours-lab-archive-earnings`, and the source of truth for what's in the watchlist)
and `candles` (a TimescaleDB hypertable of 1m OHLCV bars, tagged with which capture
window — `day_before` / `after_hours` / `day_after` — and which earnings event they
belong to). `quote_evidence` and `bar_evidence` preserve the lossless versioned gateway
contracts and provenance alongside those derived analysis candles.
`earnings_reaction_features` holds versioned, insert-only study features (see
"Persisting features"), and `research_notes` holds append-only researcher commentary,
deliberately kept in its own table so a note can never enter a computed feature.

Set `DATABASE__HOST`/`PORT`/`NAME`/`USER`/`PASSWORD` in `.env`, then:

```bash
uv run afterhours-lab-migrate
```

The primary OHLCV workflow uses exact dated `/v1/session-history` reads. For an
after-close report on date D it records four independently auditable phases:

- `earnings_regular`: D, 9:30 AM-4:00 PM ET
- `earnings_postmarket`: D, 4:00-8:00 PM ET
- `following_premarket`: next trading day, 7:00-9:30 AM ET
  (4:00-6:30 AM Pacific)
- `following_regular`: next trading day, 9:30 AM-4:00 PM ET

Raw normalized bars remain in `bar_evidence`. `earnings_ohlcv_coverage` records the
event/phase mapping, expected boundaries and minutes, observed first/last timestamps
and count, provider, retrieval time, response SHA-256, and gateway quality flags. A
missing minute is disclosed; it is never filled or inferred.

Phase boundaries use `America/New_York` and the `XNYS` schedule from
`exchange-calendars`. Holidays are skipped, regular phases use the scheduled open and
close, and postmarket begins at the scheduled close (including early-close days).
Coverage records the calendar identifier and package version used. The cron wrapper
passes the New York calendar date explicitly, which avoids assigning the 8:05 PM ET
run to the next UTC date. The premarket evidence boundary follows Schwab's available
session-history window, 7:00-9:30 AM ET (4:00-6:30 AM Pacific); it is not treated as
the broader exchange-wide 4:00 AM ET premarket.

```bash
uv run afterhours-lab-capture-ohlcv --phase earnings_regular --market-date 2026-08-26
uv run afterhours-lab-capture-ohlcv --phase earnings_postmarket --market-date 2026-08-26
uv run afterhours-lab-capture-ohlcv --phase following_premarket --market-date 2026-08-27
uv run afterhours-lab-capture-ohlcv --phase following_regular --market-date 2026-08-27

# verify all four phases, or narrow to selected symbols
uv run afterhours-lab-audit-ohlcv --date 2026-08-26
uv run afterhours-lab-audit-ohlcv --date 2026-08-26 NVDA CRM

# recover only missing historical coverage; existing phase coverage is refused
uv run afterhours-lab-capture-ohlcv --historical-backfill --symbol GEG \
  --phase earnings_regular --phase earnings_postmarket --market-date 2026-08-26
```

Historical recovery is recorded as `historical_backfill` on both raw bar evidence
and phase coverage. It never replaces an existing coverage row and must not be
described as evidence captured live. Scheduled captures are recorded separately as
`scheduled_capture`; older raw bars that predate this field remain
`legacy_unspecified`.

The older `afterhours-lab-capture` command remains available for supplementary quote
snapshots, but its short `day_before`/`after_hours`/`day_after` schedule is superseded
by the authoritative OHLCV workflow above.

For bounded one-shot recovery or research collection, the history command preserves
gateway OHLCV and upstream provenance in `bar_evidence`:

```bash
# authoritative dated session designation from /v1/session-history
uv run afterhours-lab-collect-history AAPL MSFT --date 2026-08-25

# trailing minute bars from /v1/history; session is explicitly stored as unknown
uv run afterhours-lab-collect-history AAPL --days-back 2
```

The gateway contract does not currently expose authoritative EXTO/24x5 eligibility,
exchange status, trading status, or session eligibility. Evidence rows retain these as
NULL (unknown); the app does not infer them from clock time, symbols, or quote activity.

Capture completion is recorded per symbol only after at least one usable last price was
observed. Symbols without usable quotes remain pending, and a window with no usable
quotes fails visibly instead of setting a false `*_captured` flag. A monotonic
wall-clock deadline also prevents gateway backoff from stretching a scheduled capture
indefinitely.

**Migrations are forward-only, by decision, not by omission.** `apply_migrations` in
`db/migrate.py` has no down-migration tooling, and none is planned — this matches
Butterflyguy's own migration runner (`db/migrations/run_migrations.py`), which is also
forward-only with the same checksum-guard-plus-advisory-lock shape. A migration that
needs undoing gets fixed by writing a new forward migration that corrects it, not by
rolling back; the checksum guard means an already-applied file can't be edited in place
to "become" that fix. This was a deliberate call ahead of a second migration landing,
so it isn't re-litigated later — if a future migration turns out to need a real
rollback path (e.g. before a risky schema change), that's a new decision to make
explicitly, not a gap to assume away.

## Realized earnings moves

```bash
uv run afterhours-lab-moves
uv run afterhours-lab-moves --from 2026-08-01 --to 2026-08-31
```

Reports the realized price move for every earnings event that has both its
`day_before` and `day_after` capture windows recorded (`earnings_events.day_before_captured`
and `.day_after_captured`), using only candles already persisted by
`afterhours-lab-capture` — no options/gateway chain data involved. For each event:

- **Reaction (AH)** — `day_before` close to `after_hours` close: the immediate
  reaction to the print, if that window was captured
- **Gap (Open)** — `day_before` close to `day_after` open: the overnight gap into
  the next session
- **Total Move** — `day_before` close to `day_after` close: the full round-trip
  move
- **EPS Surprise** — `eps_actual` vs `eps_estimate`, for context alongside the move

A field is left blank rather than guessed at whenever its underlying candle is
missing (e.g. the `after_hours` window wasn't captured for that event, or a window's
`*_captured` flag is set but a zero-quote poll left no candle rows).

This is the "actual" half of an eventual expected-vs-actual move comparison once
implied-vol/expected-move data exists (see the open gateway punch list); until then
it stands alone as a report on what earnings prints actually did to the stock.

## Retrospective reaction research

The first research-cycle study measures the price-discovery path from the scheduled
4:00 PM ET close through 5:45 PM ET (1:00-2:45 PM Pacific). It reads only the exact
`/v1/session-history` responses selected by `earnings_ohlcv_coverage`; it does not use
the superseded quote-poll-derived `candles` table, call the gateway, or place trades.

```bash
uv run afterhours-lab-reactions --from 2026-08-01 --to 2026-08-31
uv run afterhours-lab-reactions --from 2026-08-01 --to 2026-08-31 \
  --symbol NVDA --symbol CRM --json
```

Version 1 uses the exact 3:59-4:00 PM ET bar close as its reference price. Because
gateway minute timestamps denote interval starts, the fixed 1/5/15/30/60/105-minute
checkpoints are the closes of the 4:00/4:04/4:14/4:29/4:59/5:44 bars. A checkpoint is
left missing rather than replaced with a nearby observed bar. The reaction detector
is close-confirmed: the first minute-bar close at least 2% from the reference price is
the signal, so an intraminute high or low alone does not trigger it.

The report separates immediate continuation, spike-and-fade, delayed breakout,
whipsaw, no trigger within the study window, and insufficient evidence. It also shows
fixed-horizon returns and volume, a typical-price volume-weighted **VWAP proxy** (not
an authoritative trade VWAP), excursions, retracement, and source quality flags. JSON
output additionally retains collection modes, response hashes, stale state, and study
coverage. Early-close sessions are explicitly excluded from this first fixed-clock
study.

`afterhours-lab-reactions` stays report-only: it neither inserts derived rows nor
overwrites prior research. Persisting the same numbers is a separate, explicit step.

### Persisting features

`afterhours-lab-persist-reactions` maps a computed result onto
`earnings_reaction_features` — calculation status, missing fields, parameters, window
cutoffs, per-phase response hashes and collection modes, and a canonical input digest.

```bash
# see what would be written, without writing it
uv run afterhours-lab-persist-reactions --from 2026-08-01 --to 2026-08-31 --dry-run

# insert; rows already present for this version triple are left untouched
uv run afterhours-lab-persist-reactions --from 2026-08-01 --to 2026-08-31
```

Two properties make a stored row trustworthy:

* **Insert-only.** Every write is `ON CONFLICT DO NOTHING` on
  `(symbol, earnings_date, feature_version, detector_version, classifier_version)`, so
  a rerun can never silently change a value another study already cited. Changing a
  definition means bumping a version and inserting alongside the old generation.
* **Self-describing.** `source_evidence_sha256` digests the exact bars *and* the
  coverage identities the row was computed from, so a later rerun can prove the inputs
  were the same evidence rather than merely the same symbol and date.

The run takes its own Postgres advisory lock, so an overlapping invocation skips
rather than queueing behind a possibly wedged process.

Suggested daily research loop:

1. At 12:30 PM Pacific, freeze the after-close candidate list and record pre-close
   context without using later data.
2. From 1:00-2:45 PM, observe reactions; the current command reconstructs this window
   retrospectively after the authoritative 8:05 PM ET capture completes.
3. After capture, generate the reaction report and retain no-trigger and insufficient
   rows rather than silently dropping them.
4. Resolve following-premarket/open/close outcomes separately so future information
   never leaks into the same-day detector or classifier.
5. Review aggregates weekly by class, direction, collection mode, liquidity, and
   month before promoting any hypothesis to a formal out-of-sample backtest.

## Shared research-data layer

```
Database
   |
Typed research datasets  (afterhours_lab.research)
   |-- CLI
   |-- Jupyter
   `-- Website / API
```

`afterhours_lab.research` is the single place that reads the research tables. Nothing
outside it writes SQL against them, and nothing outside `afterhours_lab.reactions`
computes a feature, so a change to a join or a surprise definition lands once.

```python
import datetime as dt
from afterhours_lab.db.config import DatabaseSettings
from afterhours_lab.db.connection import DatabasePool
from afterhours_lab.research import EventFilter, fetch_cohort, fetch_event_detail, to_csv

cohort_filter = EventFilter(
    date_from=dt.date(2026, 1, 1),
    date_to=dt.date(2026, 8, 31),
    reaction_classes=("spike_and_fade",),
    min_abs_initial_return=3.0,
    min_coverage_ratio=0.95,
    order_by="retention_asc",
)

pool = await DatabasePool.connect(DatabaseSettings())
async with pool.acquire() as conn:
    cohort = await fetch_cohort(conn, cohort_filter)
    detail = await fetch_event_detail(conn, "NVDA", dt.date(2026, 8, 20))

print(cohort.included_count, "included;", cohort.excluded_count, "excluded")
print(to_csv(cohort.rows))
```

What it provides:

* **Event summaries** — `fetch_cohort`, `fetch_event_summary`. Earnings events joined
  to their persisted features, EPS/revenue surprise, retention, coverage phases, and
  note counts.
* **Event OHLCV paths** — `fetch_event_bars` reads bars from exactly the retrieval
  each coverage row names, so a chart and a stored number cannot disagree.
* **Data-quality information** — `fetch_quality_issues`, `fetch_operations_snapshot`.
* **Cohort filters** — `EventFilter` validates itself on construction and only emits
  parameterized SQL; sort keys come from a whitelist, never from caller-supplied text.
* **Stable exports** — `to_records`, `to_csv`, `to_jsonl`, and (with the optional
  `research` extra: `uv sync --extra research`) `to_pandas`, `to_polars`,
  `write_parquet`. Every format uses the same record shape.

Cohort reads always return `universe_count` and `included_count`, so an interface can
disclose how many events a filter excluded rather than quietly showing a subset.

Features are keyed by a version triple. `EventFilter.versions` defaults to what the
installed code computes, so a cohort never mixes generations produced by different
definitions.

## Research notebooks

`notebooks/` holds a small set of starter notebooks. **They are research clients, not
a pipeline** — none contains SQL, a threshold, or a classification rule. Every number
comes from `afterhours_lab.research`, and the connection pool is opened read-only, so
a notebook cannot write to the database even by mistake.

```bash
uv sync --extra notebooks   # jupyterlab + matplotlib + the research extra
uv run jupyter lab
```

| Notebook | What it covers |
| --- | --- |
| `01_event_exploration.ipynb` | Load a cohort, inspect one event's summary / coverage / bars / notes, exercise every export surface. |
| `02_reaction_class_distributions.ipynb` | Class counts overall, by month, by direction, by collection mode; coverage and insufficient-data rates. |
| `03_continuation_vs_fade.ipynb` | Retention distribution, detection delay vs retention, initial move vs 105-minute return, split by liquidity. |
| `04_out_of_sample_validation.ipynb` | An explicit development / out-of-sample date split, OOS untouched until the end. Prototype of the eventual study record. |

`afterhours_lab.research.notebook` is the one documented connection recipe:

```python
from afterhours_lab.research import EventFilter, fetch_cohort
from afterhours_lab.research.notebook import research_pool, show_cohort

pool = await research_pool()                 # read-only, from .env
async with pool.acquire() as conn:
    cohort = show_cohort(await fetch_cohort(conn, EventFilter(...)))
```

`show_cohort` / `describe_cohort` print the cohort's included/excluded counts, its
`EventFilter`, and its version triple — the disclosure every notebook states near the
top. Jupyter runs its own event loop, so `await` works at a cell's top level.
Commit notebooks with their outputs cleared.

## Research website

A read-only FastAPI + HTMX site over that layer, with server-built Plotly figures. It
displays and orchestrates research; it does not reimplement any calculation in
JavaScript. Plotly and htmx are vendored under `web/static/`, so the site loads no
external assets.

```bash
uv run afterhours-lab-web            # http://127.0.0.1:8055, loopback only
uv run afterhours-lab-web --reload --port 8080
```

| Page | What it answers |
| --- | --- |
| `/today` | The 12:30-2:45 PM cockpit: today's after-close reporters, pre-close reference, bid/ask/mark/spread, provisional move, first provisional threshold cross, freshness, and degradation reasons. |
| `/events` | Cross-sectional explorer: date/symbol/class/direction/move/retention/delay/liquidity/coverage/surprise filters, five cohort charts, CSV export. |
| `/events/{symbol}/{date}` | The event page: synchronized candlestick chart with the pre-close reference, ±2% bands, signal marker, PM+1/5/15/30/60/105 checkpoints, VWAP proxy, MFE/MAE, volume, and the following session; plus versions, surprise, collection mode, response hashes, notes, and a notebook snippet. |
| `/quality` | Operations only: missing capture phases, stale or unanalyzed events, backfill vs scheduled capture, and each writer's last successful write. |

Two distinctions the interface never blurs:

* **Provisional vs finalized.** Everything on `/today` is computed from whatever
  minutes happen to have been recorded so far. A row is `finalized` only once a
  persisted feature row exists.
* **Operations vs research.** Coverage gaps live on `/quality`, not in the reaction
  statistics, so a missing capture phase is never read as a market observation.

The only write path is the researcher note (`research_notes`, migration 008): append-only
discretionary commentary, stored in its own table so it can never contaminate a
computed feature. A correction is a new note, not an edit.

The site is a client of the same layer as the CLI and notebooks, and it does not own
any polling loop — capture keeps running whether or not the web server is up.

## Deploying on helios

AfterHoursLab runs on `monitoring_net`, next to `schwab-gateway` (the
`schwab_gateway_live` container is aliased as `schwab-gateway` on that network, so no
host-port tunnel is needed from inside the network). The batch entry points
(`archive-earnings`, the OHLCV captures, `persist-reactions`) are `docker compose run`
invocations from cron; `watch`/`smoke` are on-demand. The one persistent process is
the research website (`afterhours-lab-web` service), which owns no polling loop and so
can be restarted or redeployed without touching capture.

```bash
# one-time setup on helios
mkdir -p /opt/afterhours-lab && cd /opt/afterhours-lab
git clone <this repo> .
mkdir -p data
# the container runs as uid 1001 (afterhours); the bind-mounted dir must be
# writable by that uid or archive-earnings will crash on the watchlist write
sudo chown 1001:1001 data
cat > .env <<'EOF'
SCHWAB_GATEWAY_URL=http://schwab-gateway-candidate:8012
SCHWAB_GATEWAY_API_KEY=<the afterhours-lab gateway key>
FINNHUB_API_KEY=<finnhub key>
DATABASE__HOST=timescaledb
DATABASE__PORT=5432
DATABASE__NAME=afterhours_lab
DATABASE__USER=afterhours_lab
DATABASE__PASSWORD=<the afterhours_lab db password>
# optional: Telegram alert on archive-earnings failure — see .env.example
TELEGRAM_BOT_TOKEN=<optional>
TELEGRAM_CHAT_ID=<optional>
EOF
docker compose build
docker compose run --rm afterhours-lab afterhours-lab-migrate

# cron: see "Cron install" below — archive-earnings, the OHLCV capture windows, and
# persist-reactions are installed the same way, from infra/cron/

# the research website: a long-running service, loopback-published on 127.0.0.1:8055
docker compose up -d afterhours-lab-web

# on-demand: watch the archived list live
docker compose run --rm afterhours-lab afterhours-lab-watch --watchlist /app/data/watchlist.json

# on-demand: gateway smoke test
docker compose run --rm afterhours-lab afterhours-lab-smoke
```

The `afterhours-lab-web` service carries `restart: unless-stopped`, so it comes back
after a reboot or a crash. Redeploy it with `docker compose up -d --build
afterhours-lab-web`; that is independent of the cron batch jobs.

`./data` on the host persists `watchlist.json` and `last_run_status.json` across
container runs (the image itself is stateless and rebuilt from source each deploy). A
quick check of the last cron outcome, without opening the log:

```bash
cat /opt/afterhours-lab/data/last_run_status.json
```

### Cron install (archive-earnings + authoritative OHLCV phases + reaction features)

The OHLCV wrapper performs bounded point-in-time reads after each phase is available:

| Entry | ET time | What it does |
| --- | --- | --- |
| `archive_earnings` | 8:30 AM | Refreshes `earnings_events` and `watchlist.json` |
| `capture_ohlcv` | 9:35 AM | Prior event's following-day premarket |
| `capture_ohlcv` | 4:05 PM | Today's regular + prior event's following regular |
| `capture_ohlcv` | 8:05 PM | Today's complete postmarket |
| `persist_reactions` | 8:25 PM | Computes + inserts reaction features from the coverage the 8:05 PM run just wrote |

`persist_reactions` re-scans a short trailing window (four days), not just today, so a
capture that finished late is still picked up the next evening. It is insert-only and
takes its own advisory lock, so the overlap with a slow 8:05 PM run is harmless.

**`archive_earnings` is the producer the other three read** — `capture_ohlcv.py` picks its
symbols out of `earnings_events`, so a day this doesn't run is a day nothing gets
captured. It goes at 8:30 AM ET to sit between the two things it serves: late enough
that the previous evening's after-close prints have published actuals, and before the
9:30 AM regular-session open for today's after-close names.

All five are timing-sensitive enough that a plain single-UTC-slot cron line isn't good
enough across a DST transition — each one follows Butterflyguy's
UTC-dual-slot-plus-wrapper pattern (`tools/run_morning_scan_cron.sh`): the crontab
fires at two UTC hour candidates and the wrapper script no-ops unless it's actually the
target `America/New_York` clock time.

Install each with the same idempotent, additive pattern Butterflyguy's own cron
snippets use — this only ever touches the line matching its own wrapper script name,
so it's safe to run alongside Butterflyguy's or any other app's crontab entries on the
same host:

```bash
crontab -l 2>/dev/null | grep -v run_archive_earnings_cron.sh | cat - infra/cron/archive_earnings.cron | crontab -
crontab -l 2>/dev/null | grep -v run_capture_ohlcv_cron.sh | cat - infra/cron/capture_ohlcv.cron | crontab -
crontab -l 2>/dev/null | grep -v run_persist_reactions_cron.sh | cat - infra/cron/persist_reactions.cron | crontab -
```

When upgrading from the quote-polling schedule, remove its three legacy wrapper lines
before installing `capture_ohlcv.cron`; running both schedules would duplicate traffic
and preserve two different notions of coverage.

The collector also takes one non-blocking Postgres advisory lock for the whole batch.
An overlapping cron or manual run skips instead of issuing duplicate gateway reads.

### Log rotation

The cron entries append to `/opt/afterhours-lab/*.log` forever.
`infra/logrotate/afterhours-lab` rotates them weekly, keeping 8 generations — the most
recent stays uncompressed (`delaycompress`) so it's still greppable, the rest are gzipped:

```bash
sudo install -m 0644 -o root -g root infra/logrotate/afterhours-lab /etc/logrotate.d/afterhours-lab
sudo logrotate -v --force /etc/logrotate.d/afterhours-lab   # verify: actually rotates once
```

Verify with `-v --force`, not `--debug`: debug mode reports "considering log …" and
then skips every real check, so a broken config still looks like it worked. The config
carries `su billy billy` because the deploy directory is group-writable by `billy` —
without it logrotate refuses every log under it with "parent directory has insecure
permissions".

It uses `copytruncate` rather than `create` so any active cron/container stdout file
descriptor remains attached to the current pathname while rotation occurs.

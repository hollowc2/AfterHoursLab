# AfterHoursLab

A read-only market-data lab application. It has no Schwab credentials of its own and
never will — all market data comes exclusively from the standalone internal gateway at
[hollowc2/SchwabGateway](https://github.com/hollowc2/SchwabGateway) over its HTTP API
(`/v1/quotes`, `/v1/spot`, `/v1/chain`), authenticated with a pre-issued internal API key.

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

Two tables: `earnings_events` (one row per flagged symbol/date — populated by
`afterhours-lab-archive-earnings`, and the source of truth for what's in the watchlist)
and `candles` (a TimescaleDB hypertable of 1m OHLCV bars, tagged with which capture
window — `day_before` / `after_hours` / `day_after` — and which earnings event they
belong to). The `*_captured` flags on `earnings_events` stay `FALSE` until a candles
recording pipeline exists to set them — see below.

Set `DATABASE__HOST`/`PORT`/`NAME`/`USER`/`PASSWORD` in `.env`, then:

```bash
uv run afterhours-lab-migrate
```

The actual recording pipeline (pulling 1m candles into these tables) depends on a
gateway-side historical-candles endpoint that doesn't exist yet — see the open design
question in SchwabGateway about a point-in-time date+session history contract vs. its
existing trailing-window `/v1/history`. This schema is ready ahead of that.

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

## Deploying on helios

AfterHoursLab runs as its own container on `monitoring_net`, next to `schwab-gateway`
(the `schwab_gateway_live` container is aliased as `schwab-gateway` on that network, so
no host-port tunnel is needed from inside the network). There's no persistent process —
`archive-earnings` runs on a daily cron, and `watch`/`smoke` are on-demand tools.

```bash
# one-time setup on helios
mkdir -p /opt/afterhours-lab && cd /opt/afterhours-lab
git clone <this repo> .
mkdir -p data
# the container runs as uid 1001 (afterhours); the bind-mounted dir must be
# writable by that uid or archive-earnings will crash on the watchlist write
sudo chown 1001:1001 data
cat > .env <<'EOF'
SCHWAB_GATEWAY_URL=http://schwab-gateway:8011
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

# cron: see "Cron install" below — archive-earnings and all three capture windows
# are installed the same way, from infra/cron/

# on-demand: watch the archived list live
docker compose run --rm afterhours-lab afterhours-lab-watch --watchlist /app/data/watchlist.json

# on-demand: gateway smoke test
docker compose run --rm afterhours-lab afterhours-lab-smoke
```

`./data` on the host persists `watchlist.json` and `last_run_status.json` across
container runs (the image itself is stateless and rebuilt from source each deploy). A
quick check of the last cron outcome, without opening the log:

```bash
cat /opt/afterhours-lab/data/last_run_status.json
```

### Cron install (archive-earnings + the three capture windows)

Four cron entries, each timed to a specific point in the trading day (see the matching
`tools/run_*_cron.sh` for why each timing was chosen):

| Entry | ET time | What it does |
| --- | --- | --- |
| `archive_earnings` | 8:30 AM | Refreshes `earnings_events` and `watchlist.json` |
| `capture_day_before` | 3:55 PM | Pre-earnings closing candles |
| `capture_after_hours` | 4:00 PM | The earnings-reaction window |
| `capture_day_after` | 9:28 AM | Next session, open through close |

**`archive_earnings` is the producer the other three read** — `capture.py` picks its
symbols out of `earnings_events`, so a day this doesn't run is a day nothing gets
captured. It goes at 8:30 AM ET to sit between the two things it serves: late enough
that the previous evening's after-close prints have published actuals, and well ahead
of the 3:55 PM `day_before` capture, which needs tomorrow's names already in the table.

All four are timing-sensitive enough that a plain single-UTC-slot cron line isn't good
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
crontab -l 2>/dev/null | grep -v run_capture_day_before_cron.sh | cat - infra/cron/capture_day_before.cron | crontab -
crontab -l 2>/dev/null | grep -v run_capture_after_hours_cron.sh | cat - infra/cron/capture_after_hours.cron | crontab -
crontab -l 2>/dev/null | grep -v run_capture_day_after_cron.sh | cat - infra/cron/capture_day_after.cron | crontab -
```

`capture_status.json` next to `watchlist.json` in `./data` tracks the outcome of the
most recent capture run, same idea as `last_run_status.json` for archive-earnings.

### Log rotation

The four cron entries append to `/opt/afterhours-lab/*.log` forever. `infra/logrotate/
afterhours-lab` rotates them weekly, keeping 8 compressed generations:

```bash
sudo install -m 0644 -o root -g root infra/logrotate/afterhours-lab /etc/logrotate.d/afterhours-lab
sudo logrotate --debug /etc/logrotate.d/afterhours-lab   # dry run, prints what it would do
```

It uses `copytruncate` rather than `create` on purpose: the `day_after` capture holds
its stdout redirect open for a 395-minute run, so a rotation that renamed the file out
from under it would leave the container writing to an unlinked inode for the rest of
the session.

**Per-window re-entrancy guard.** `capture.py` holds a non-blocking advisory lock for
its entire poll — minutes to hours — so the key has to be per-window
(`capture_lock_key(window)` = `CAPTURE_LOCK_KEY` + a pinned per-window offset), not one
key shared by all three. The windows overlap on the clock: `day_after` starts at
9:28 AM ET and polls for 395 minutes, straight through the 3:55 PM `day_before` and
4:00 PM `after_hours` fires. With a single shared key those two hit the "already
running" branch and skipped — recorded as a *successful* run — on every day a
`day_after` capture was active. Each window now only guards against a stuck run of
itself, which means all three can be polling the gateway at once around 4:00 PM.

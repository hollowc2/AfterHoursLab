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
# today's after-close earnings: recorded to earnings_events, watchlist.json refreshed
uv run afterhours-lab-archive-earnings

# a date range, without writing to earnings_events or the watchlist
uv run afterhours-lab-archive-earnings --from 2026-08-19 --to 2026-08-21 --dry-run
```

Requires both `FINNHUB_API_KEY` and the `DATABASE__*` settings below — `--dry-run` is
the only mode that doesn't touch either the database or the watchlist file.

This is the base data source for the lab; other earnings-driven studies can build on
top of it later.

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
cat > .env <<'EOF'
SCHWAB_GATEWAY_URL=http://schwab-gateway:8011
SCHWAB_GATEWAY_API_KEY=<the afterhours-lab gateway key>
FINNHUB_API_KEY=<finnhub key>
DATABASE__HOST=timescaledb
DATABASE__PORT=5432
DATABASE__NAME=afterhours_lab
DATABASE__USER=afterhours_lab
DATABASE__PASSWORD=<the afterhours_lab db password>
EOF
docker compose build
docker compose run --rm afterhours-lab afterhours-lab-migrate

# cron (crontab -e), pre-market weekdays — single UTC slot, drifts an hour across DST
30 12 * * 1-5 cd /opt/afterhours-lab && docker compose run --rm afterhours-lab >> /opt/afterhours-lab/archive_earnings.log 2>&1

# on-demand: watch the archived list live
docker compose run --rm afterhours-lab afterhours-lab-watch --watchlist /app/data/watchlist.json

# on-demand: gateway smoke test
docker compose run --rm afterhours-lab afterhours-lab-smoke
```

`./data` on the host persists `watchlist.json` across container runs (the image itself
is stateless and rebuilt from source each deploy).

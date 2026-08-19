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

Base behavior: find symbols reporting earnings **after market close** and archive
them into the persisted watchlist, so the watch viewer picks them up.

```bash
# today's after-close earnings, archived into ./watchlist.json
uv run afterhours-lab-archive-earnings

# a date range, without touching the watchlist
uv run afterhours-lab-archive-earnings --from 2026-08-19 --to 2026-08-21 --dry-run
```

This is the base data source for the lab; other earnings-driven studies can build on
top of it later.

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
EOF
docker compose build

# cron (crontab -e), pre-market weekdays — single UTC slot, drifts an hour across DST
30 12 * * 1-5 cd /opt/afterhours-lab && docker compose run --rm afterhours-lab >> /opt/afterhours-lab/archive_earnings.log 2>&1

# on-demand: watch the archived list live
docker compose run --rm afterhours-lab afterhours-lab-watch --watchlist /app/data/watchlist.json

# on-demand: gateway smoke test
docker compose run --rm afterhours-lab afterhours-lab-smoke
```

`./data` on the host persists `watchlist.json` across container runs (the image itself
is stateless and rebuilt from source each deploy).

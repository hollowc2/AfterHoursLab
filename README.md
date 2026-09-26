<p align="center">
  <img src="logo.jpg" alt="AfterHoursLab" width="600">
</p>

# AfterHoursLab

AfterHoursLab records how stocks react to after-close earnings reports. Every result can be traced back to its source data.

It never places trades. It captures market data, measures each reaction, and tests hypotheses under preregistered rules:

**raw quotes → verified price bars → reaction features → next-session outcomes → preregistered studies → one sealed out-of-sample result**

## Setup

```bash
uv sync
cp .env.example .env   # set SCHWAB_GATEWAY_URL, SCHWAB_GATEWAY_API_KEY, DATABASE__*
uv run afterhours-lab-migrate
```

Run the tests and linter:

```bash
uv run pytest
uv run ruff check .
```

## Commands

| Task | Command |
| --- | --- |
| Add upcoming after-close earnings to the watchlist | `afterhours-lab-archive-earnings` |
| Capture price bars for one phase | `afterhours-lab-capture-ohlcv --phase earnings_regular --market-date 2026-08-26` |
| Check capture coverage | `afterhours-lab-audit-ohlcv --date 2026-08-26` |
| Preview reactions without saving | `afterhours-lab-reactions --from 2026-08-01 --to 2026-08-31` |
| Save reaction features | `afterhours-lab-persist-reactions --from 2026-08-01 --to 2026-08-31` |
| Save next-session outcomes | `afterhours-lab-persist-outcomes --from 2026-08-01 --to 2026-08-31` |
| Register or evaluate a study | `afterhours-lab-study register --spec studies/example.json` |
| Exclude older low-liquidity events | `afterhours-lab-backfill-liquidity [--dry-run]` |
| Retire events whose earnings date has moved | `afterhours-lab-reconcile-calendar --from 2026-08-01 --to 2026-09-30 [--dry-run]` |
| Watch the live watchlist | `afterhours-lab-watch` |
| Test the gateway connection | `afterhours-lab-smoke` |

### Data is never overwritten

Every write adds new rows. Running a command again never replaces earlier data, and a changed definition is saved as a new version instead of editing the old one.

### Liquidity filter

`archive-earnings` skips any stock that averaged less than $20M a day in dollar volume over the last 10 sessions (`MIN_AVG_DOLLAR_VOLUME`). Skipped stocks are never captured, monitored, or counted in `/quality`. If the liquidity check fails, the stock is kept.

`backfill-liquidity` applies the same rule to events recorded before the filter existed. It sets `earnings_events.liquidity_excluded_at`, which removes the event from every research view. The underlying data stays in the database for audit. The gateway reports only current liquidity, so this check approximates liquidity at the time of each event rather than measuring it.

### Rescheduled earnings

Companies often move their earnings date after announcing it. On every refresh, `archive-earnings` checks the calendar. If the same symbol, fiscal year, and quarter now appears on a different date, the old event gets `earnings_events.superseded_at` and is removed from the watchlist, capture, the live monitor, and every research view. A symbol that is simply missing from the calendar is not treated as moved. `reconcile-calendar` applies the same rule to events recorded earlier.

## Database

The database is PostgreSQL with TimescaleDB. `afterhours-lab-migrate` applies migrations forward only and has no rollback. The main tables are:

- `earnings_events`
- `candles`
- `quote_evidence` and `bar_evidence`
- `earnings_ohlcv_coverage`
- `earnings_reaction_features`
- `following_session_outcomes`
- `study_*`
- `research_notes` (append-only)

## Research access

The CLI, notebooks, and website all read data through `afterhours_lab.research`, so each definition is written once.

```python
from afterhours_lab.research import EventFilter, fetch_cohort
from afterhours_lab.research.notebook import research_pool, show_cohort

pool = await research_pool()
async with pool.acquire() as conn:
    cohort = show_cohort(await fetch_cohort(conn, EventFilter(date_from=..., date_to=...)))
```

```bash
uv sync --extra notebooks && uv run jupyter lab   # notebooks/ read data only, never write SQL
uv run afterhours-lab-web                          # http://127.0.0.1:8055 (/today, /events, /quality)
```

## Deployment

AfterHoursLab runs under Docker Compose on the `monitoring_net` network, next to `schwab-gateway`.

- **Batch jobs** (`archive-earnings`, price-bar capture, `persist-reactions`, `persist-outcomes`) run on cron. See `infra/cron/`.
- **Services** (the website and the live quote monitor) run continuously with `restart: unless-stopped`.

Cron and logrotate configs are in `infra/`.

```bash
docker compose build
docker compose run --rm afterhours-lab afterhours-lab-migrate
docker compose up -d afterhours-lab-web afterhours-lab-monitor
```

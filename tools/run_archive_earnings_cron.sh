#!/usr/bin/env bash
# Refresh earnings_events and the persisted watchlist at 8:30 AM
# America/New_York on weekdays. This is the producer every capture window
# consumes: capture.py picks its symbols out of earnings_events, so nothing
# gets captured on a day this didn't run.
#
# 8:30 AM ET is chosen to sit between the two things it has to serve:
# late enough that the previous evening's after-close prints have published
# actuals (which this run backfills onto yesterday's rows), and well ahead of
# the 3:55 PM day_before capture, which needs *tomorrow's* after-close names
# already in the table. The CLI's default --from/--to span covers both
# (previous through next trading day), so no explicit dates are passed here.
#
# Vixie cron ignores CRON_TZ in user crontabs — schedule this at 12:30 and
# 13:30 UTC so one slot hits 8:30 AM during EDT and EST.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "08:30" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
exec docker compose run --rm afterhours-lab \
  afterhours-lab-archive-earnings --watchlist /app/data/watchlist.json

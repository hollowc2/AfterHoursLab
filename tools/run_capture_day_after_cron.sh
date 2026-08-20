#!/usr/bin/env bash
# Gate day_after earnings-candle capture to 9:28 AM America/New_York on
# weekdays, running through the close (~395 minutes) so this single window
# covers both the post-earnings open (moves.py's post_open) and close
# (post_close) — capture.py marks day_after_captured TRUE at the end of one
# run, so a second same-day fire would find nothing pending and no-op; the
# whole session has to be one continuous poll. Vixie cron ignores CRON_TZ in
# user crontabs — schedule this at 13:28 and 14:28 UTC so one slot hits
# 9:28 AM during EDT and EST.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "09:28" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
exec docker compose run --rm afterhours-lab \
  afterhours-lab-capture --window day_after --duration-minutes 395 \
  --watchlist /app/data/watchlist.json

#!/usr/bin/env bash
# Gate after_hours earnings-candle capture to 4:00 PM America/New_York on
# weekdays — this app only tracks after-close ("amc") reporters (see
# earnings.after_close_entries), so the print and its immediate reaction land
# in the 4:00-5:30 PM ET window this covers. Vixie cron ignores CRON_TZ in
# user crontabs — schedule this at 20:00 and 21:00 UTC so one slot hits
# 4:00 PM during EDT and EST.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "16:00" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
exec docker compose run --rm afterhours-lab \
  afterhours-lab-capture --window after_hours --duration-minutes 90 \
  --watchlist /app/data/watchlist.json

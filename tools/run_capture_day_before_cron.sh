#!/usr/bin/env bash
# Gate day_before earnings-candle capture to 3:55 PM America/New_York on
# weekdays (just ahead of the 4:00 PM ET close, so the captured window's last
# candle is the pre-earnings closing price). Vixie cron ignores CRON_TZ in
# user crontabs — schedule this at 19:55 and 20:55 UTC so one slot hits
# 3:55 PM during EDT and EST.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "15:55" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
exec docker compose run --rm afterhours-lab \
  afterhours-lab-capture --window day_before --duration-minutes 8 \
  --watchlist /app/data/watchlist.json

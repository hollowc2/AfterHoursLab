#!/usr/bin/env bash
# Refresh earnings_events and the persisted watchlist at 8:30 AM, 3:55 PM, and
# 7:55 PM America/New_York on weekdays. This is the producer every capture
# window consumes: capture.py picks its symbols out of earnings_events, so a
# late calendar addition must land before the 4:05 PM and 8:05 PM captures.
#
# The morning run populates the day's universe and backfills yesterday's
# actuals. The two later refreshes close the race where the upstream calendar
# adds a reporter after 8:30 AM but before its price-history capture.
#
# Vixie cron ignores CRON_TZ in user crontabs. The crontab provides both UTC
# candidates for each Eastern wall-clock time; this gate accepts only the real
# 08:30, 15:55, or 19:55 invocation.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
case "$(TZ=America/New_York date +%H:%M)" in
  08:30|15:55|19:55) ;;
  *) exit 0 ;;
esac

cd /opt/afterhours-lab
echo "--- $(date -u +%FT%TZ) $(basename "$0")"
exec docker compose run --rm afterhours-lab \
  afterhours-lab-archive-earnings --watchlist /app/data/watchlist.json

#!/usr/bin/env bash
# Close following-session outcomes at 4:25 PM America/New_York on exchange weekdays.
# Two UTC slots plus the local-time guard make this DST-safe. The trailing ten-day
# scan covers weekends, exchange holidays, and late authoritative captures.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "16:25" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
from_date="$(TZ=America/New_York date -d '10 days ago' +%F)"
to_date="$(TZ=America/New_York date +%F)"
echo "--- $(date -u +%FT%TZ) $(basename "$0")"
exec docker compose run --rm afterhours-lab afterhours-lab-persist-outcomes \
  --from "$from_date" --to "$to_date"

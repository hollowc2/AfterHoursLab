#!/usr/bin/env bash
# Compute and insert earnings-reaction features for the after-close reporters whose
# authoritative post-market OHLCV has landed. Runs at 8:25 PM America/New_York on
# weekdays — 20 minutes after the 8:05 PM ET post-market capture wrapper.
#
# Insert-only and advisory-locked, so re-persisting a day is a no-op. It re-scans a
# short trailing window rather than only "today" so a capture that finished late
# (gateway backoff — this app is priority: background) is still picked up the next
# evening instead of being lost.
#
# Vixie cron ignores CRON_TZ in user crontabs; 8:25 PM ET is always the next UTC
# day, so schedule at 00:25 and 01:25 UTC and let one slot hit during EDT, the
# other during EST. The wrapper no-ops unless it is actually 20:25 in New York.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi
if [[ "$(TZ=America/New_York date +%H:%M)" != "20:25" ]]; then
  exit 0
fi

cd /opt/afterhours-lab
from_date="$(TZ=America/New_York date -d '4 days ago' +%F)"
to_date="$(TZ=America/New_York date +%F)"
echo "--- $(date -u +%FT%TZ) $(basename "$0")"
exec docker compose run --rm afterhours-lab afterhours-lab-persist-reactions \
  --from "$from_date" --to "$to_date"

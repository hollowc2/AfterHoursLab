#!/usr/bin/env bash
# Capture authoritative minute OHLCV around after-close earnings. UTC dual slots are
# gated by the actual America/New_York clock for DST safety.
set -euo pipefail

if [[ "$(TZ=America/New_York date +%u)" -gt 5 ]]; then
  exit 0
fi

cd /opt/afterhours-lab
market_date="$(TZ=America/New_York date +%F)"
case "$(TZ=America/New_York date +%H:%M)" in
  09:35)
    phases=(--phase following_premarket)
    ;;
  16:05)
    phases=(--phase earnings_regular --phase following_regular)
    ;;
  20:05)
    # The regular-session phase is retried here for symbols added to the earnings
    # calendar after the 4:05 PM run. capture_phase skips symbols with existing
    # coverage, so this only fills genuinely missing regular evidence.
    phases=(--phase earnings_regular --phase earnings_postmarket)
    ;;
  *)
    exit 0
    ;;
esac

exec docker compose run --rm afterhours-lab afterhours-lab-capture-ohlcv \
  --market-date "$market_date" "${phases[@]}"

#!/usr/bin/env bash
# Refresh Heroic's GOG and Amazon (Prime) library caches without opening its window.
#
# Heroic only rewrites store_cache/gog_library.json and nile_library.json when it
# starts (checked 2026-10-23: both refresh ~10 s after launch, also with --no-gui),
# and their auth can't be refreshed by the bundled CLIs on their own. So:
#   - Heroic not running: start it hidden (--no-gui), wait for both files to be
#     rewritten, then quit it again (SIGTERM to its main process).
#   - Heroic already running (you may be playing): never touch it. Fine if the
#     caches are under a day old; otherwise exit 1 so the portal's Scans page
#     flags it — use Heroic's own "refresh library" then.
#
# Called by refresh.sh before the Heroic importer (job: heroic-sync).

set -uo pipefail

APP=com.heroicgameslauncher.hgl
CACHE="$HOME/.var/app/$APP/config/heroic/store_cache"
FILES=("$CACHE/gog_library.json" "$CACHE/nile_library.json")
TIMEOUT=180          # seconds to wait for the refresh
FRESH=86400          # "fresh enough" when Heroic is already open: 1 day

heroic_main() { pgrep -f "^/app/bin/heroic/heroic( |$)" | head -1; }
age() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) )); }

if ! flatpak info "$APP" >/dev/null 2>&1; then
    echo "Heroic (flatpak $APP) is not installed"; exit 1
fi

if [[ -n "$(heroic_main)" ]]; then
    oldest=0
    for f in "${FILES[@]}"; do a=$(age "$f"); (( a > oldest )) && oldest=$a; done
    if (( oldest < FRESH )); then
        echo "Heroic is already open; GOG/Prime libraries refreshed $(( oldest / 3600 )) h ago — OK"
        exit 0
    fi
    echo "Heroic is open (left alone), but GOG/Prime libraries are $(( oldest / 86400 )) days old — refresh the library in Heroic"
    exit 1
fi

start=$(date +%s)
systemd-run --user --collect --quiet --unit="game-catalog-heroic-sync-$start" \
    flatpak run "$APP" --no-gui >/dev/null 2>&1 \
    || { echo "could not start Heroic"; exit 1; }
echo "started Heroic hidden (--no-gui); waiting for the GOG/Prime libraries…"

ok=0
for (( waited = 0; waited < TIMEOUT; waited += 5 )); do
    sleep 5
    fresh=1
    for f in "${FILES[@]}"; do
        [[ $(stat -c %Y "$f" 2>/dev/null || echo 0) -ge $start ]] || fresh=0
    done
    if (( fresh )); then ok=1; break; fi
done
sleep 5    # let Heroic finish writing the rest of its cache

main=$(heroic_main)
if [[ -n "$main" ]]; then
    kill -TERM "$main"
    for _ in $(seq 1 20); do [[ -z "$(heroic_main)" ]] && break; sleep 1; done
fi

if (( ok )); then
    echo "GOG and Prime libraries refreshed after ~${waited}s; Heroic closed again"
else
    echo "GOG/Prime libraries were not refreshed within ${TIMEOUT}s (signed out of GOG or Amazon in Heroic?)"
    exit 1
fi

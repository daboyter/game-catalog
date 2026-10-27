#!/usr/bin/env bash
# Refresh the game catalog database.
# Run manually, via systemd --user timer (game-catalog-refresh.timer), or from
# the portal's Scans page (⚙ → Rescan).
#
# What it does:
#   1. Sync Epic library headlessly via legendary (no display needed, ~30s), and
#      refresh GOG/Prime by starting Heroic hidden for a few seconds (tools/heroic_sync.sh)
#   2. Run the store importers (Steam API, Heroic caches, Xodus, ES-DE, Lutris, Game Pass),
#      then hide store entries that aren't games (not-games.tsv)
#   3. Check Game Pass titles' PC packages with Xodus Nexus (new titles only; no sign-in),
#      then load DroidDeck availability (scan results + predictions for new titles)
#   4. Fill store release years, match new games to IGDB, fetch browsing metadata,
#      match new ES-DE ROMs to IGDB (per platform) and rebuild their tags
#   5. Reload compat-notes.tsv
#
# Every step is recorded in the scan_runs table (runlog.py): start, finish,
# ok/failed, and the last error line. Each run gets its own folder of per-step
# logs under ~/.local/share/game-catalog/logs/<timestamp>/, which the portal's
# Scans page shows. A failed step never stops the others.
#
# What it does NOT do automatically:
#   - DroidDeck scanning: needs the Odin 2 on adb from the Mac (tools/droiddeck-scan);
#     its committed results are loaded here, with Xodus predictions in between scans.
#
# Usage:
#   bash refresh.sh [--only STORE] [--trigger timer|manual|portal]
#                   [--skip-igdb] [--skip-epic-sync] [--max-igdb N]
#   --only STORE: quick refresh of one importer (steam heroic xodus esde lutris
#                 gamepass) plus the matching/metadata steps, e.g. after buying a game.

set -uo pipefail     # no -e: step() handles failures itself

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The Python venv with requirements.txt installed; set GAME_CATALOG_VENV to use another one.
VENV_DIR="${GAME_CATALOG_VENV:-$HOME/game-catalog-venv}"
VENV_PYTHON="$VENV_DIR/bin/python3"
LOG_ROOT="$HOME/.local/share/game-catalog/logs"
ALL_IMPORTERS=(steam heroic xodus esde lutris gamepass)

# ── flags ─────────────────────────────────────────────────────────────────────
SKIP_IGDB=0
SKIP_EPIC_SYNC=0
MAX_IGDB=""
ONLY=""
TRIGGER="manual"
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-igdb)      SKIP_IGDB=1; shift ;;
        --skip-epic-sync) SKIP_EPIC_SYNC=1; shift ;;
        --max-igdb)       MAX_IGDB="$2"; shift 2 ;;
        --only)           ONLY="$2"; shift 2 ;;
        --trigger)        TRIGGER="$2"; shift 2 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done
# systemd timer runs pass no flags; tell them apart from a shell run
[[ -n "${INVOCATION_ID:-}" && "$TRIGGER" == "manual" ]] && TRIGGER="timer"

if [[ -n "$ONLY" ]]; then
    if [[ ! " ${ALL_IMPORTERS[*]} " =~ " $ONLY " ]]; then
        echo "--only must be one of: ${ALL_IMPORTERS[*]}"; exit 1
    fi
    IMPORTERS=("$ONLY")
    [[ "$ONLY" != "heroic" ]] && SKIP_EPIC_SYNC=1
else
    IMPORTERS=("${ALL_IMPORTERS[@]}")
fi

# ── one refresh at a time (timer vs a rescan started from the portal) ─────────
mkdir -p "$LOG_ROOT"
exec 9>"$LOG_ROOT/.refresh.lock"
if ! flock -n 9; then
    echo "Another refresh is already running — not starting a second one."
    exit 75   # EX_TEMPFAIL
fi

# ── setup ─────────────────────────────────────────────────────────────────────
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$LOG_ROOT/$STAMP"
mkdir -p "$RUN_DIR"
LOGFILE="$RUN_DIR/refresh.log"
cd "$SCRIPT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

if [[ ! -x "$VENV_PYTHON" ]]; then
    log "ERROR: Python venv not found at $VENV_DIR"
    exit 1
fi

record() {   # record JOB STATUS STARTED LOG [MESSAGE]
    "$VENV_PYTHON" runlog.py "$@" --run "$STAMP" --trigger "$TRIGGER" >/dev/null 2>&1 \
        || log "  (could not record $1 in scan_runs)"
}

FAILED=()
step() {     # step JOB "Description" command args...
    local job="$1" desc="$2"; shift 2
    local steplog="$RUN_DIR/$job.log" started rc
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log "$desc"
    record "$job" running "$started" "$steplog"
    "$@" > >(tee -a "$steplog" "$LOGFILE") 2>&1
    rc=$?
    wait   # let tee finish writing
    if [[ $rc -eq 0 ]]; then
        record "$job" ok "$started" "$steplog"
    else
        log "  $job FAILED (exit $rc; continuing)"
        record "$job" failed "$started" "$steplog" "exit $rc: $(grep -v '^\s*$' "$steplog" | tail -1 | cut -c1-200)"
        FAILED+=("$job")
    fi
}

REFRESH_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "=== game-catalog refresh started (${ONLY:-full}, trigger: $TRIGGER) ==="
record refresh running "$REFRESH_STARTED" "$LOGFILE" "${ONLY:+only $ONLY}"

# ── Step 1: Epic sync via legendary ──────────────────────────────────────────
if [[ $SKIP_EPIC_SYNC -eq 0 ]]; then
    LEGENDARY_BIN=$(find /var/lib/flatpak/app/com.heroicgameslauncher.hgl \
        -name legendary -type f 2>/dev/null | head -1)
    LEGENDARY_CONFIG="$HOME/.var/app/com.heroicgameslauncher.hgl/config/heroic/legendaryConfig/legendary"
    if [[ -n "$LEGENDARY_BIN" && -d "$LEGENDARY_CONFIG" ]]; then
        step epic-sync "Syncing Epic library via legendary…" \
            env LEGENDARY_CONFIG_PATH="$LEGENDARY_CONFIG" "$LEGENDARY_BIN" list-games -q
    else
        log "  legendary not found — skipping Epic sync"
    fi
fi

# Heroic rewrites its GOG/Prime library caches only when it starts: open it
# hidden for a few seconds (never touches an already-open Heroic)
if [[ " ${IMPORTERS[*]} " =~ " heroic " ]]; then
    step heroic-sync "Refreshing GOG/Prime libraries via Heroic…" bash tools/heroic_sync.sh
fi

# ── Step 2: importers ─────────────────────────────────────────────────────────
for importer in "${IMPORTERS[@]}"; do
    step "import-$importer" "Importer: $importer" "$VENV_PYTHON" "importers/${importer}.py"
done

# Store entries that aren't games (not-games.tsv): engines, tools, soundtracks
step not-games "Hiding store entries that aren't games…" "$VENV_PYTHON" not_games.py

# ── Step 3: Game Pass PC packages via Xodus Nexus (no sign-in; new titles only) ─
if [[ " ${IMPORTERS[*]} " =~ " gamepass " ]]; then
    step xodus-resolve "Checking Game Pass PC packages with Xodus…" \
        "$VENV_PYTHON" tools/xodus_resolve.py
fi
# DroidDeck availability: committed scan results + predictions for new titles
step droiddeck-import "Loading DroidDeck availability…" "$VENV_PYTHON" tools/droiddeck_import.py

# ── Step 4: matching and metadata ─────────────────────────────────────────────
if [[ $SKIP_IGDB -eq 0 ]]; then
    # Store release years feed merge.py's same-title collision tiebreak.
    step store-years "Filling store release years…" "$VENV_PYTHON" store_years.py
    MAX_ARG=()
    [[ -n "$MAX_IGDB" ]] && MAX_ARG=(--max-games "$MAX_IGDB")
    step igdb-match "Matching new games to IGDB…" "$VENV_PYTHON" merge.py "${MAX_ARG[@]}"
    # Browsing metadata (genres/themes/modes/…, ratings, time-to-beat) for the portal's filters
    step igdb-details "Fetching browsing metadata…" "$VENV_PYTHON" enrich_details.py
    # ES-DE ROMs: new ROMs only (cached IGDB platform lists); rom_tags, store-game links
    step igdb-roms "Matching ES-DE ROMs to IGDB…" "$VENV_PYTHON" match_roms.py
fi

# ── Step 5: compat notes (after IGDB, so igdb_id-keyed rows resolve) ─────────
step compat-notes "Loading compat notes…" "$VENV_PYTHON" load-compat-notes.py

# ── done ──────────────────────────────────────────────────────────────────────
if [[ ${#FAILED[@]} -gt 0 ]]; then
    log "=== refresh finished with FAILED steps: ${FAILED[*]} ==="
    record refresh failed "$REFRESH_STARTED" "$LOGFILE" "failed: ${FAILED[*]}"
    exit 1     # so `systemctl --user status` shows it too
fi
log "=== refresh complete ==="
record refresh ok "$REFRESH_STARTED" "$LOGFILE" "${ONLY:+only $ONLY}"

# keep the last 30 runs' logs
ls -1d "$LOG_ROOT"/2*/ 2>/dev/null | sort | head -n -30 | xargs -r rm -rf

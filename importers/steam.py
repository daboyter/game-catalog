#!/usr/bin/env python3
"""
Steam importer — calls GetOwnedGames and upserts into the game catalog DB.

Reads credentials from ~/.config/game-catalog/steam.env (STEAM_API_KEY, STEAM_ID).
Caches the raw API response at ~/.local/share/game-catalog/cache/steam_owned.json
(TTL 1 hour by default); use --force-refresh to bypass.

Usage:
    ~/game-catalog-venv/bin/python3 importers/steam.py [--force-refresh]
"""

import argparse, json, pathlib, sys, time, urllib.request, urllib.parse
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, upsert_game

# ── credentials ──────────────────────────────────────────────────────────────

env_path = pathlib.Path.home() / ".config/game-catalog/steam.env"
if not env_path.exists():
    sys.exit(f"ERROR: {env_path} not found")

_env = {}
for _line in env_path.read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, _, v = _line.partition("=")
        _env[k.strip()] = v.strip()

API_KEY  = _env.get("STEAM_API_KEY", "")
STEAM_ID = _env.get("STEAM_ID", "")
if not API_KEY or not STEAM_ID:
    sys.exit("ERROR: STEAM_API_KEY and STEAM_ID must be set in steam.env")

# ── cache ─────────────────────────────────────────────────────────────────────

CACHE_DIR  = pathlib.Path.home() / ".local/share/game-catalog/cache"
CACHE_FILE = CACHE_DIR / "steam_owned.json"
CACHE_TTL  = 3600  # seconds


def fetch_owned(force=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            print(f"  [steam] using cache ({int(age)}s old)")
            return json.loads(CACHE_FILE.read_text())

    print("  [steam] calling GetOwnedGames API…")
    params = urllib.parse.urlencode({
        "key":                       API_KEY,
        "steamid":                   STEAM_ID,
        "include_appinfo":           1,
        "include_played_free_games": 1,
        "format":                    "json",
    })
    url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?{params}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    CACHE_FILE.write_text(json.dumps(data))
    games = data.get("response", {}).get("games", [])
    print(f"  [steam] {len(games)} games fetched")
    return data


# ── import ────────────────────────────────────────────────────────────────────

def run(force=False):
    data  = fetch_owned(force)
    games = data.get("response", {}).get("games", [])

    new_games  = 0
    upserted_o = 0
    upserted_p = 0

    with open_db() as db:
        for g in games:
            appid    = str(g["appid"])
            title    = g.get("name", f"Steam:{appid}")
            playtime = g.get("playtime_forever", 0)   # minutes
            last_ts  = g.get("rtime_last_played", 0)
            last_iso = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_ts))
                if last_ts else None
            )
            cover_url = (
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_231x87.jpg"
            )

            game_id, created = upsert_game(db, title)
            if created:
                new_games += 1

            db.execute("""
                INSERT INTO ownership
                    (game_id, store, store_id, store_title, kind, is_installed,
                     art_cover_url, raw_json)
                VALUES (?, 'steam', ?, ?, 'purchased', 0, ?, ?)
                ON CONFLICT (store, store_id) DO UPDATE SET
                    store_title   = excluded.store_title,
                    art_cover_url = excluded.art_cover_url,
                    raw_json      = excluded.raw_json,
                    imported_at   = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """, (game_id, appid, title, cover_url, json.dumps(g)))
            upserted_o += 1

            if playtime > 0:
                db.execute("""
                    INSERT INTO playtime (game_id, source, store_id, minutes_total, last_played_at)
                    VALUES (?, 'steam', ?, ?, ?)
                    ON CONFLICT (game_id, source) DO UPDATE SET
                        minutes_total  = excluded.minutes_total,
                        last_played_at = excluded.last_played_at,
                        updated_at     = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """, (game_id, appid, playtime, last_iso))
                upserted_p += 1

        db.commit()

    print(f"  [steam] done: {new_games} new games, "
          f"{upserted_o} ownership rows, {upserted_p} playtime rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    run(force=args.force_refresh)

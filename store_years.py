#!/usr/bin/env python3
"""
Fill ownership.release_year from each store's own release date.

merge.py uses this as a tiebreak when IGDB has several games with the exact
same normalized title (e.g. Valve's "Portal" 2007 vs Activision's "Portal" 1986):
the candidate whose IGDB year is closest to the store year wins.

Sources (all public, no auth, no keys):
  steam    — store.steampowered.com/api/appdetails  release_date.date ("Oct 10, 2007")
  gog      — api.gog.com/v2/games/<id>  _embedded.product.globalReleaseDate
             (NOT api.gog.com/products/<id> release_date — that is the date the
              game was added to GOG, e.g. Betrayer shows 2023 instead of 2014)
  amazon   — Heroic nile_library.json  extra.releaseDate (local file, no request)
  gamepass — displaycatalog.mp.microsoft.com  MarketProperties[0].OriginalReleaseDate
             (sometimes off by a year — Xbox listing date — still fine as a tiebreak)
  epic     — none. legendary metadata only has creationDate, which is when the
             catalog item was created on Epic, not when the game came out.
  lutris   — none (only launchers and a handful of games).

Lookups are cached in ~/.local/share/game-catalog/cache/store_years.json as
{"store:store_id": year_or_null}, including misses, so each title is fetched
once ever. Delete the file to force re-fetching.

Steam's appdetails endpoint is rate limited (~200 requests / 5 min), so we
sleep 1.5s between Steam calls. First run takes ~5 min for ~200 Steam games.

Usage:
    ~/game-catalog-venv/bin/python3 store_years.py [--dry-run]
"""

import argparse, json, pathlib, re, time, urllib.error, urllib.request
from _db import open_db

CACHE_FILE = pathlib.Path.home() / ".local/share/game-catalog/cache/store_years.json"
AMAZON_LIB = (pathlib.Path.home()
              / ".var/app/com.heroicgameslauncher.hgl/config/heroic/store_cache/nile_library.json")

STEAM_URL    = "https://store.steampowered.com/api/appdetails?appids={id}&filters=release_date"
GOG_URL      = "https://api.gog.com/v2/games/{id}?locale=en-US"
GAMEPASS_URL = ("https://displaycatalog.mp.microsoft.com/v7.0/products"
                "?bigIds={ids}&market=US&languages=en-us&MS-CV=0")

STEAM_DELAY   = 1.5
DEFAULT_DELAY = 0.2
GAMEPASS_BATCH = 20


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "game-catalog/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _year(s):
    """Pull a plausible 4-digit year out of any date string; None if absent."""
    if not s:
        return None
    m = re.search(r"\b(19[5-9]\d|20\d\d)\b", str(s))
    return int(m.group(1)) if m else None


# ── per-store fetchers: each returns {store_id: year_or_None} ────────────────

def fetch_steam(ids):
    out = {}
    for i, appid in enumerate(ids):
        if i:
            time.sleep(STEAM_DELAY)
        try:
            d = _get_json(STEAM_URL.format(id=appid)).get(str(appid), {})
            out[appid] = _year(d.get("data", {}).get("release_date", {}).get("date")) \
                         if d.get("success") else None
        except Exception as e:
            # Leave uncached on network errors / 429 so the next run retries
            print(f"  [store_years] steam {appid}: {e}")
    return out


def fetch_gog(ids):
    out = {}
    for i, gid in enumerate(ids):
        if i:
            time.sleep(DEFAULT_DELAY)
        try:
            d = _get_json(GOG_URL.format(id=gid))
            out[gid] = _year(d.get("_embedded", {}).get("product", {}).get("globalReleaseDate"))
        except urllib.error.HTTPError as e:
            # 404 = delisted / not in v2 API; cache the miss. Other errors retry next run.
            if e.code == 404:
                out[gid] = None
            else:
                print(f"  [store_years] gog {gid}: {e}")
        except Exception as e:
            print(f"  [store_years] gog {gid}: {e}")
    return out


def fetch_gamepass(ids):
    out = {}
    for i in range(0, len(ids), GAMEPASS_BATCH):
        batch = ids[i:i + GAMEPASS_BATCH]
        if i:
            time.sleep(DEFAULT_DELAY)
        try:
            products = _get_json(GAMEPASS_URL.format(ids=",".join(batch))).get("Products", [])
        except Exception as e:
            print(f"  [store_years] gamepass batch {i}: {e}")
            continue
        for p in products:
            mp = (p.get("MarketProperties") or [{}])[0]
            out[p.get("ProductId")] = _year(mp.get("OriginalReleaseDate"))
        for sid in batch:
            out.setdefault(sid, None)
    return out


def fetch_amazon(ids):
    if not AMAZON_LIB.exists():
        print(f"  [store_years] {AMAZON_LIB} not found — skipping amazon")
        return {}
    lib = json.loads(AMAZON_LIB.read_text()).get("library", [])
    by_id = {g.get("app_name"): _year(g.get("extra", {}).get("releaseDate")) for g in lib}
    return {sid: by_id.get(sid) for sid in ids}


FETCHERS = {
    "steam":    fetch_steam,
    "gog":      fetch_gog,
    "gamepass": fetch_gamepass,
    "amazon":   fetch_amazon,
}


def run(db, dry_run=False):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    for store, fetch in FETCHERS.items():
        ids = [r[0] for r in db.execute(
            "SELECT store_id FROM ownership WHERE store = ? AND kind != 'launcher'", (store,))]
        todo = [sid for sid in ids if f"{store}:{sid}" not in cache]
        print(f"  [store_years] {store}: {len(ids)} rows, {len(todo)} to look up")
        if todo:
            for sid, year in fetch(todo).items():
                cache[f"{store}:{sid}"] = year
            if not dry_run:
                CACHE_FILE.write_text(json.dumps(cache, indent=0, sort_keys=True))

        filled = 0
        for sid in ids:
            year = cache.get(f"{store}:{sid}")
            if year and not dry_run:
                db.execute("UPDATE ownership SET release_year = ? WHERE store = ? AND store_id = ?",
                           (year, store, sid))
                filled += 1
        print(f"  [store_years] {store}: {filled} rows have a year")

    if not dry_run:
        db.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fill ownership.release_year from store data")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write cache or DB")
    args = parser.parse_args()
    with open_db() as db:
        run(db, dry_run=args.dry_run)

#!/usr/bin/env python3
"""
Smoke-test for IGDB credentials.

Reads ~/.config/game-catalog/igdb.env, gets a Twitch OAuth token,
then runs three working tests:
  1. Title search for "Portal"
  2. Steam uid lookup for Half-Life 2 (appid 220)  — uid only, no category filter
  3. Genre fetch for a known game (IGDB id 233 = Half-Life 2)

NOTE on external_games / category:
  IGDB stores many external_game rows with category=null, including Steam entries.
  Filtering by `category=1` (Steam) misses them. The reliable approach is:
    - Look up by uid alone: `where uid="<appid>"`
    - Pick the result whose uid is a short integer string (not a YouTube ID or GUID)
  The importers use title-based search as the primary matching strategy, with
  uid lookup as a secondary check for Steam and GOG.

Usage:
    ~/game-catalog-venv/bin/python3 test_igdb.py
"""

import os, sys, json, pathlib, time, urllib.request, urllib.parse

# --- load credentials from igdb.env ---
env_path = pathlib.Path.home() / ".config/game-catalog/igdb.env"
if not env_path.exists():
    sys.exit(f"ERROR: {env_path} not found.")

for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

CLIENT_ID     = os.environ.get("IGDB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", "")

if not CLIENT_ID or CLIENT_ID == "paste_your_client_id_here":
    sys.exit("ERROR: Fill in IGDB_CLIENT_ID in ~/.config/game-catalog/igdb.env first.")
if not CLIENT_SECRET or CLIENT_SECRET == "paste_your_client_secret_here":
    sys.exit("ERROR: Fill in IGDB_CLIENT_SECRET in ~/.config/game-catalog/igdb.env first.")


# --- token management ---
def get_token():
    """
    Returns a cached Bearer token, requesting a fresh one from Twitch if
    the cache is missing or has less than 1 hour left.
    Token lives at ~/.local/share/game-catalog/cache/igdb_token.json.
    """
    cache_dir = pathlib.Path.home() / ".local/share/game-catalog/cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "igdb_token.json"

    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if cached.get("expires_at", 0) > time.time() + 3600:
            return cached["access_token"]

    params = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }).encode()
    req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=params, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    token = {
        "access_token": data["access_token"],
        "expires_at":   time.time() + data.get("expires_in", 3600),
    }
    cache_file.write_text(json.dumps(token))
    print(f"  [token] new token, expires in {data.get('expires_in', 0) // 86400} days")
    return token["access_token"]


def igdb_query(token, endpoint, body):
    req = urllib.request.Request(
        f"https://api.igdb.com/v4/{endpoint}",
        data=body.encode(),
        headers={
            "Client-ID":     CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# --- run tests ---
print("Getting OAuth token...")
try:
    token = get_token()
    print(f"  OK — token starts with {token[:8]}…\n")
except Exception as e:
    sys.exit(f"ERROR getting token: {e}")


# Test 1: title search
print('Test 1: title search for "Portal"')
try:
    results = igdb_query(token, "games",
        'fields id,name,first_release_date,genres.name; '
        'search "Portal"; where version_parent = null; limit 5;')
    for g in results:
        genres = [x["name"] for x in g.get("genres", [])]
        ts = g.get("first_release_date", 0)
        year = time.strftime("%Y", time.gmtime(ts)) if ts else "?"
        print(f"  id={g['id']:>8}  {year}  {g['name']}")
        if genres:
            print(f"           genres: {', '.join(genres)}")
    print(f"  -> {len(results)} results  ✓\n")
except Exception as e:
    print(f"  ERROR: {e}\n")


# Test 2: Steam uid lookup (no category filter — many IGDB entries have category=null)
# A Steam appid is a short integer string; YouTube IDs start with letters, Xbox IDs are GUIDs.
# We pick the result that looks like a plain integer.
print("Test 2: Steam uid lookup — Half-Life 2 (Steam appid 220)")
try:
    results = igdb_query(token, "external_games",
        'fields game,name,category,uid; where uid = "220"; limit 10;')
    steam_entry = None
    for r in results:
        uid = r.get("uid", "")
        # Steam appids are short digit-only strings; filter out YouTube IDs and GUIDs
        if uid.isdigit() and len(uid) <= 10:
            steam_entry = r
        print(f"  uid={uid!r:30s} category={r.get('category')}  game_id={r.get('game')} name={r.get('name')}")
    if steam_entry:
        print(f"  -> Steam entry found: game_id={steam_entry['game']}  ✓")
    else:
        print("  -> No short-integer uid found — check results above")
    print()
except Exception as e:
    print(f"  ERROR: {e}\n")


# Test 3: genre fetch for a known IGDB game (id=233 = Half-Life 2)
print("Test 3: genres for Half-Life 2 (IGDB id=233)")
try:
    results = igdb_query(token, "games",
        "fields id,name,genres.name,first_release_date,cover.url; where id=233; limit 1;")
    for g in results:
        genres = [x["name"] for x in g.get("genres", [])]
        ts = g.get("first_release_date", 0)
        year = time.strftime("%Y", time.gmtime(ts)) if ts else "?"
        cover = g.get("cover", {}).get("url", "none")
        print(f"  id={g['id']}  name={g['name']}  year={year}")
        print(f"  genres: {genres}")
        print(f"  cover:  {cover}")
    print(f"  -> {len(results)} results  ✓\n")
except Exception as e:
    print(f"  ERROR: {e}\n")


print("All tests done.")

#!/usr/bin/env python3
"""
IGDB enrichment pass — looks up all unmatched games in the catalog
and fills in igdb_id, canonical_title, cover_url, genres_json,
first_release_year, and description from IGDB.

Reads IGDB credentials from ~/.config/game-catalog/igdb.env.
Token cached at ~/.local/share/game-catalog/cache/igdb_token.json.
Per-game IGDB results cached in ~/.local/share/game-catalog/cache/igdb_games/<igdb_id>.json
so reruns only look up genuinely new games.

Rate limit: IGDB allows 4 requests/second; we sleep 0.26s between calls
(two calls per game: exact-name lookup, then keyword search; up to four
more for an unmatched title that may be an edition).

Matching strategy:
  0. Before any searching, igdb-pins.tsv (store, store_id → igdb_id) is applied:
     pinned games get exactly that IGDB record and are never searched or
     rematched. Use it when the automatic pick is wrong.
  1. Look up IGDB by exact name (case-insensitive `where name ~`), then add
     keyword-search hits (APIcalypse `search`); both limited to parent games
     (version_parent = null).
  2. Among results, accept ONLY an exact title match (same string after
     ™/®/© removal, & → and, lowercase, strip punctuation + leading articles;
     see match_key()).
  2b. If SEVERAL results match exactly (same-title collision, e.g. Valve's
     "Portal" 2007 vs Activision's "Portal" 1986), first drop Port/Bundle
     entries and zero-rating entries (when others remain), then pick the one whose IGDB
     release year is closest to the store year (MIN ownership.release_year,
     filled by store_years.py). Ties, or no store year (Epic), fall back to
     the most-rated IGDB entry (total_rating_count). Each collision decision
     is printed so you can audit it; override with igdb-pins.tsv.
  2c. Editions. Stores sell "Control Ultimate Edition", "Civilization III:
     Complete", "Forza Horizon 5 Standard Edition"; IGDB files the game under
     its plain name. When step 2 finds nothing:
       - if IGDB lists the exact store title as an edition of another game
         (version_parent set, e.g. "Sid Meier's Civilization III: Complete"),
         the parent game is the match;
       - otherwise edition words at the end of the title (EDITION_SUFFIXES:
         "Standard Edition", "Gold Edition", "(Classic)", "(Windows)", "for
         Windows + Launcher", "(Beta)", …) are stripped and steps 1–2b run
         again on the rest — still an exact match on that base title.
     If you already own the base game, the edition merges into its card.
  3. If no exact match: record the most similar candidates in possible-matches.tsv
     (written alongside this script, in igdb-pins.tsv format) so you can
     confirm them by copying lines into igdb-pins.tsv.
     The entry is marked igdb_matched_at = now but igdb_id = NULL.

Duplicate cards: if a game's match (pinned or searched) is an IGDB game that
another catalog row already holds, the two rows are one game sold under
different store titles, so they're merged (_db.merge_games) and the old
title is kept in game_aliases. To merge two entries by hand, pin both store
entries to the same igdb_id.

IMPORTANT: No fuzzy/Levenshtein merging is done automatically.
"portal" vs "portal 2" and "half-life" vs "half-life 2" are only 1-2 edits
apart — automatic fuzzy merging would silently corrupt the catalog.
Confirm near-matches by copying their lines into igdb-pins.tsv.

Already-matched games (igdb_id IS NOT NULL) are skipped, unless --rematch is
given: then every matched game is re-searched and re-picked with the rules
above, and its row is updated only if the chosen igdb_id changed.
Games where igdb_matched_at IS NOT NULL are skipped (tried before; re-run
with --retry-failed to attempt them again).

ES-DE ROMs (esde_entries table) are matched separately, platform-aware,
by match_roms.py.

Usage:
    ~/game-catalog-venv/bin/python3 merge.py [--max-games N] [--retry-failed] [--rematch] [--dry-run]
"""

import argparse, difflib, json, pathlib, re, sqlite3, sys, time, urllib.request, urllib.parse
from _db import open_db, normalize_title, merge_games, list_file

# ── credentials and token ─────────────────────────────────────────────────────

def _load_env(path):
    env = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

env_path = pathlib.Path.home() / ".config/game-catalog/igdb.env"
if not env_path.exists():
    sys.exit(f"ERROR: {env_path} not found — IGDB enrichment unavailable")
_env = _load_env(env_path)
CLIENT_ID     = _env.get("IGDB_CLIENT_ID", "")
CLIENT_SECRET = _env.get("IGDB_CLIENT_SECRET", "")
if not CLIENT_ID or CLIENT_ID == "paste_your_client_id_here":
    sys.exit("ERROR: fill in IGDB_CLIENT_ID in ~/.config/game-catalog/igdb.env")

IGDB_CACHE_DIR = pathlib.Path.home() / ".local/share/game-catalog/cache/igdb_games"
PINS_FILE      = list_file("igdb-pins.tsv")

# Fields requested for every IGDB game lookup (search and pin-by-id)
IGDB_FIELDS = ("id,name,first_release_date,genres.name,cover.url,summary,"
               "total_rating_count,game_type.type,url")

# Edition words stripped from the END of a store title (repeatedly, so
# "Standard 40th Anniversary Edition (Windows)" goes in steps) when the full
# title has no exact IGDB match. Regexes, case-insensitive; an optional
# " - " / ": " before them is removed too. Keep them specific: a word that is
# part of real titles ("Remastered", "Collection") doesn't belong here.
EDITION_SUFFIXES = [
    r"(standard|gold|deluxe|ultimate|complete|definitive|celebration|elite|"
    r"eldritch|cross-gen|survivor|game of the year|goty|\d+(st|nd|rd|th) anniversary|anniversary)"
    r"( edition| bundle)?",
    r"20 year celebration",
    r"complete story",
    r"legacy mode",
    r"\((classic|windows|pc|beta|test branch|\d{4})\)",
    r"public testing",
    r"tech beta",
    r"for windows \+ launcher",
    r"for pc",
    r"pc",                    # Game Pass Blizzard titles: "Diablo® IV PC", "Overwatch® PC"
]
_EDITION_RE = re.compile(r"\s*(?:[-–:]\s*)?(?:" + "|".join(EDITION_SUFFIXES) + r")\s*$", re.I)

# possible-matches.tsv filtering (review list only; never auto-matches)
REVIEW_MIN_SIMILARITY = 0.6
# IGDB game_type.type names for add-ons (note IGDB spells packs "Pack / Addon")
REVIEW_SKIP_TYPES     = {"DLC", "Expansion", "Mod", "Episode", "Season", "Pack / Addon", "Update"}


def get_token():
    cache_dir  = pathlib.Path.home() / ".local/share/game-catalog/cache"
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
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token", data=params, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    token = {"access_token": data["access_token"],
             "expires_at":   time.time() + data.get("expires_in", 3600)}
    cache_file.write_text(json.dumps(token))
    return token["access_token"]


def igdb_query(token, endpoint, body, retries=3):
    req = urllib.request.Request(
        f"https://api.igdb.com/v4/{endpoint}",
        data=body.encode(),
        headers={
            "Client-ID":     CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return []


def _igdb_year(r):
    ts = r.get("first_release_date")
    return time.gmtime(ts).tm_year if ts else None


def match_key(title):
    """
    Title key for comparing against IGDB names. Stricter cleanup than
    normalize_title (which is the DB's cross-store key and must not change):
      - ™ ® © removed first; NFKD would otherwise turn ™ into the letters "tm"
        ("Second Extinction™" → "secondextinctiontm" never matched)
      - "&" → "and" ("Hood: Outlaws & Legends" vs "Hood Outlaws and Legends")
    Still an exact comparison — no fuzzy matching.
    """
    title = re.sub(r"[™®©]", "", title or "").replace("&", " and ")
    return normalize_title(title)


def strip_edition(title):
    """"Forza Horizon 5 Standard Edition" → "Forza Horizon 5" (EDITION_SUFFIXES)."""
    original = title = re.sub(r"[™®©]", "", title).strip()
    prev = None
    while prev != title:
        prev, title = title, _EDITION_RE.sub("", title).strip()
    return title or original            # a title that is ALL edition words stays as it was


def search(token, title):
    """
    IGDB parent games for `title`: exact-name lookup (case-insensitive), then
    keyword-search hits. IGDB's keyword search ranks big games below obscure
    ones surprisingly often — "Control", "The Finals", "Anno 1800" never
    appeared in its top 10 — hence the exact lookup first.
    """
    safe = re.sub(r"[™®©]", "", title).replace('"', '').replace('\\', '').strip()
    results = igdb_query(token, "games",
                         f'fields {IGDB_FIELDS}; where name ~ "{safe}" & version_parent = null; limit 10;')
    time.sleep(0.26)
    seen = {r["id"] for r in results}
    results += [r for r in igdb_query(token, "games",
                                      f'fields {IGDB_FIELDS}; search "{safe}"; where version_parent = null; limit 10;')
                if r["id"] not in seen]
    time.sleep(0.26)
    return results


def edition_parent(token, title):
    """The parent game if IGDB lists `title` exactly as an edition of one, else None."""
    safe = re.sub(r"[™®©]", "", title).replace('"', '').replace('\\', '').strip()
    editions = [r for r in igdb_query(token, "games",
                                      f'fields name,version_parent; where name ~ "{safe}" & version_parent != null; limit 10;')
                if match_key(r.get("name", "")) == match_key(title)]
    time.sleep(0.26)
    if not editions:
        return None
    parent = igdb_query(token, "games", f'fields {IGDB_FIELDS}; where id = {editions[0]["version_parent"]};')
    time.sleep(0.26)
    return parent[0] if parent else None


def pick_match(results, key, store_year):
    """
    Return (match, exact_candidates). match is None when nothing matches exactly.
    key is match_key(our title). With several exact matches: closest year to
    store_year wins, then most-rated.
    """
    exact = [r for r in results if match_key(r.get("name", "")) == key]
    if len(exact) <= 1:
        return (exact[0] if exact else None), exact

    # Narrow the pool before comparing years. Each filter only applies if it
    # leaves at least one candidate.
    #  - Ports/bundles: Game Pass dates are the Xbox listing date, so without this
    #    an old game pulls toward its later console port (Doom 3 → 2019 bundle).
    #  - Unrated entries: same-name obscure games with zero IGDB ratings
    #    (Limbo 2024, Wolfenstein 3D 2014, Hades 1995) are never what a store sells
    #    when a rated candidate exists. Remakes/remasters are kept (RE2 2019).
    pool = [r for r in exact
            if (r.get("game_type") or {}).get("type") not in ("Port", "Bundle")] or exact
    pool = [r for r in pool if r.get("total_rating_count")] or pool

    def rank(r):
        y = _igdb_year(r)
        # 9999 = unknown distance: sorts after any real year distance
        dist = abs(y - store_year) if (store_year and y) else 9999
        return (dist, -(r.get("total_rating_count") or 0))

    return min(pool, key=rank), exact


def write_match(db, game_id, match, canon, dry_run):
    """
    Copy an IGDB game record onto games.id = game_id and return "matched".

    If another games row already holds this igdb_id, the two rows are the same
    game under different store titles (Game Pass "Fallout" vs Epic "Fallout: A
    Post Nuclear Role Playing Game"): fold game_id into that row instead and
    return "merged". igdb_id is UNIQUE, so one IGDB game = one card.
    """
    igdb_id    = match["id"]
    igdb_title = match.get("name", canon)
    year       = _igdb_year(match)
    genres     = [g["name"] for g in match.get("genres", [])]
    cover      = match.get("cover", {}).get("url", None)
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    if cover:
        cover = cover.replace("t_thumb", "t_cover_big")
    desc = match.get("summary", None)

    holder = db.execute("SELECT id, canonical_title FROM games WHERE igdb_id = ? AND id != ?",
                        (igdb_id, game_id)).fetchone()
    if holder:
        print(f"  [merge] {canon!r} is the same IGDB game ({igdb_id}) as "
              f"{holder['canonical_title']!r} — merging into one card")
        if not dry_run:
            merge_games(db, game_id, holder["id"])
        return "merged"

    if dry_run:
        return "matched"

    # Cache the raw result so reruns don't re-query
    (IGDB_CACHE_DIR / f"{igdb_id}.json").write_text(json.dumps(match))
    db.execute("""
        UPDATE games SET
            igdb_id            = ?,
            canonical_title    = ?,
            first_release_year = ?,
            genres_json        = ?,
            cover_url          = ?,
            description        = ?,
            igdb_matched_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
            updated_at         = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        WHERE id = ?
    """, (igdb_id, igdb_title, year,
          json.dumps(genres) if genres else None,
          cover, desc, game_id))
    return "matched"


# ── manual pins (igdb-pins.tsv) ───────────────────────────────────────────────

def load_pins(db):
    """
    Read igdb-pins.tsv → {game_id: igdb_id}. Rows are keyed on (store, store_id)
    so they survive DB rebuilds; a row whose store entry isn't in the DB (not
    imported yet, or delisted) is reported and ignored.
    """
    pins = {}
    if not PINS_FILE.exists():
        return pins
    for lineno, line in enumerate(PINS_FILE.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if cols[0] == "store":          # header row
            continue
        if len(cols) < 3 or not cols[2].strip().isdigit():
            print(f"  [merge] igdb-pins.tsv:{lineno}: expected store<TAB>store_id<TAB>igdb_id — skipping")
            continue
        store, store_id, igdb_id = cols[0].strip(), cols[1].strip(), int(cols[2])
        row = db.execute("SELECT game_id FROM ownership WHERE store = ? AND store_id = ?",
                         (store, store_id)).fetchone()
        if not row:
            print(f"  [merge] igdb-pins.tsv:{lineno}: no ownership row for {store}:{store_id} — skipping")
            continue
        pins[row["game_id"]] = igdb_id
    return pins


def apply_pins(db, token, pins, dry_run):
    """Point each pinned game at its pinned igdb_id (only where it differs)."""
    todo = {gid: iid for gid, iid in pins.items()
            if db.execute("SELECT igdb_id FROM games WHERE id = ?", (gid,)).fetchone()["igdb_id"] != iid}
    if not todo:
        return
    results = igdb_query(token, "games",
                         f'fields {IGDB_FIELDS}; where id = ({",".join(map(str, todo.values()))});'
                         f' limit {len(todo)};')
    by_id = {r["id"]: r for r in results}
    for game_id, igdb_id in todo.items():
        canon = db.execute("SELECT canonical_title FROM games WHERE id = ?", (game_id,)).fetchone()[0]
        if igdb_id not in by_id:
            print(f"  [merge] pin {canon!r}: igdb {igdb_id} not found on IGDB — skipping")
            continue
        if write_match(db, game_id, by_id[igdb_id], canon, dry_run) == "matched":
            print(f"  [merge] pin {canon!r} → igdb {igdb_id} ({by_id[igdb_id].get('name')}, "
                  f"{_igdb_year(by_id[igdb_id])})")
    if not dry_run:
        db.commit()


# ── main ──────────────────────────────────────────────────────────────────────

def run(db: sqlite3.Connection, max_games=None, retry_failed=False,
        rematch=False, dry_run=False):
    token = get_token()
    IGDB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Pins first; pinned games are then left out of searching/rematching entirely.
    pins = load_pins(db)
    if pins:
        print(f"[merge] {len(pins)} pinned games (igdb-pins.tsv)")
        apply_pins(db, token, pins, dry_run)

    # Games to enrich: exclude ownership.kind='launcher', exclude ES-DE-only rows.
    # store_year = earliest store release year across all stores that list the game
    # (re-releases and remasters are usually later, so MIN is closest to the original).
    if rematch:
        cond = "g.igdb_id IS NOT NULL"
    elif retry_failed:
        cond = "g.igdb_id IS NULL"
    else:
        cond = "g.igdb_id IS NULL AND g.igdb_matched_at IS NULL"
    q = f"""
        SELECT g.id, g.normalized_title, g.canonical_title, g.igdb_id,
               (SELECT MIN(o.release_year) FROM ownership o WHERE o.game_id = g.id) AS store_year
        FROM games g
        WHERE {cond}
          AND EXISTS (SELECT 1 FROM ownership o WHERE o.game_id = g.id AND o.kind != 'launcher')
    """

    rows = [r for r in db.execute(q).fetchall() if r["id"] not in pins]
    if max_games:
        rows = rows[:max_games]

    print(f"[merge] {len(rows)} games to {'re-check' if rematch else 'enrich'} via IGDB"
          + (" (dry run)" if dry_run else ""))

    # possible-matches.tsv: near-misses for manual review
    possible_matches_path = pathlib.Path(__file__).parent / "possible-matches.tsv"
    possible_matches = []

    matched = merged = failed = collisions = changed = 0

    for i, row in enumerate(rows):
        game_id    = row["id"]
        canon      = row["canonical_title"]
        store_year = row["store_year"]
        old_igdb   = row["igdb_id"]

        if i % 25 == 0:
            print(f"  {i}/{len(rows)}…")

        try:
            results = search(token, canon)
            # Exact normalized-title match only; year/popularity tiebreak on collisions
            key = match_key(canon)
            match, exact = pick_match(results, key, store_year)
            if not match and not rematch:
                # 2c: an edition — IGDB's own edition entry, else the title minus edition words
                parent = edition_parent(token, canon)
                if parent:
                    match, exact = parent, [parent]
                    print(f"  [merge] edition {canon!r} → igdb {parent['id']} ({parent.get('name')})")
                elif (base := strip_edition(canon)) != re.sub(r"[™®©]", "", canon).strip():
                    more = search(token, base)
                    key = match_key(base)
                    match, exact = pick_match(more, key, store_year)
                    results += more
                    if match:
                        print(f"  [merge] edition {canon!r} → {base!r}: igdb {match['id']}")
        except Exception as e:
            print(f"  [merge] IGDB error for {canon!r}: {e}")
            continue

        if len(exact) > 1:
            collisions += 1
            others = ", ".join(f"{r['id']}/{_igdb_year(r)}" for r in exact if r is not match)
            print(f"  [merge] collision {canon!r} (store year {store_year}): "
                  f"picked {match['id']}/{_igdb_year(match)} over {others}")

        if rematch:
            if not match or match["id"] == old_igdb:
                continue   # no exact match now, or same pick — keep the existing match
            changed += 1
            print(f"  [merge] rematch {canon!r}: igdb {old_igdb} → {match['id']}")

        if match:
            if write_match(db, game_id, match, canon, dry_run) == "merged":
                merged += 1
            else:
                matched += 1

        else:
            # Record the most similar candidates for manual review (display only —
            # nothing here is matched automatically). Add-ons are never the
            # base game, and low-similarity hits are search noise.
            own = db.execute("SELECT store, store_id FROM ownership "
                             "WHERE game_id = ? AND kind != 'launcher' ORDER BY store LIMIT 1",
                             (game_id,)).fetchone()
            scored = []
            for r in results:
                if (r.get("game_type") or {}).get("type") in REVIEW_SKIP_TYPES:
                    continue
                sim = difflib.SequenceMatcher(None, key, match_key(r.get("name", ""))).ratio()
                if sim >= REVIEW_MIN_SIMILARITY:
                    scored.append((sim, r))
            for sim, r in sorted(scored, key=lambda s: -s[0])[:3]:
                possible_matches.append({
                    "store":      own["store"] if own else "",
                    "store_id":   own["store_id"] if own else "",
                    "our_title":  canon,
                    "igdb_id":    r.get("id"),
                    "igdb_title": r.get("name"),
                    "igdb_year":  _igdb_year(r) or "",
                    "similarity": f"{sim:.2f}",
                    "url":        r.get("url", ""),
                })

            # Mark as tried so we don't retry on next run
            if not dry_run:
                db.execute("""
                    UPDATE games SET
                        igdb_matched_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                        updated_at      = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE id = ?
                """, (game_id,))
            failed += 1

        if i % 50 == 49 and not dry_run:
            db.commit()

    if not dry_run:
        db.commit()

    # Write possible-matches.tsv. The first four columns are exactly the
    # igdb-pins.tsv format, so confirming a match = copying its line there.
    # Only rewritten on runs that looked at unmatched games, so a normal
    # refresh with nothing new doesn't blank a file you're reviewing.
    if failed and not dry_run:
        with possible_matches_path.open("w") as f:
            f.write("# Near-miss IGDB candidates for games with no exact title match.\n"
                    "# REGENERATED by merge.py — don't edit here.\n"
                    "#\n"
                    "# To confirm a match: copy its whole line into igdb-pins.tsv\n"
                    "# (at most one line per game). Extra columns are ignored there.\n"
                    "# Check with the IGDB url if unsure. Skip games with no right answer.\n"
                    "#\n")
            f.write("store\tstore_id\tigdb_id\tnote\tsimilarity\turl\n")
            last = None
            for m in sorted(possible_matches, key=lambda m: m["our_title"].lower()):
                if last is not None and m["our_title"] != last:
                    f.write("\n")      # blank line between games
                last = m["our_title"]
                note = f"{m['our_title']} → {m['igdb_title']} ({m['igdb_year'] or '?'})"
                f.write(f"{m['store']}\t{m['store_id']}\t{m['igdb_id']}\t{note}\t"
                        f"{m['similarity']}\t{m['url']}\n")
        games_with = len({m["our_title"] for m in possible_matches})
        print(f"[merge] {len(possible_matches)} near-miss candidates for {games_with} games "
              f"→ {possible_matches_path}")

    if rematch:
        print(f"[merge] rematch done: {changed} games re-pointed, {collisions} collisions seen")
        return
    print(f"[merge] done: {matched} matched, {merged} merged into existing games, "
          f"{failed} unmatched, {collisions} same-title collisions "
          f"(--retry-failed to retry; check possible-matches.tsv)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IGDB enrichment for game catalog")
    parser.add_argument("--max-games",    type=int, default=None,
                        help="Only process N games (for testing)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Retry games previously tried but not matched")
    parser.add_argument("--rematch",      action="store_true",
                        help="Re-check already-matched games with the collision tiebreak")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Run lookups but do not write to DB or cache")
    args = parser.parse_args()
    with open_db() as db:
        run(db, max_games=args.max_games,
            retry_failed=args.retry_failed,
            rematch=args.rematch,
            dry_run=args.dry_run)

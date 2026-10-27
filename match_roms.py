#!/usr/bin/env python3
"""
IGDB matching for ES-DE ROMs — fills esde_entries.igdb_id and the rom_tags
table, so the ROM grid gets the same theme / mode / tag filters as the store
games, and links ROMs to store games that are the same IGDB game.
Run after enrich_details.py (it reuses its genre map and tag groups).

Platform-aware matching
-----------------------
Each ES-DE system maps to its IGDB platforms (SYSTEMS below). Instead of
asking IGDB about every ROM by name (IGDB's `name = (...)` list is
case-sensitive, and ScreenScraper capitalizes differently: "Wonder Boy In
Monster Land"), the whole IGDB game list of each platform is downloaded once
— about 20,000 games in ~45 requests of 500 — and cached for CATALOG_DAYS in
    ~/.local/share/game-catalog/cache/igdb_platforms/<platform id>.json
Downloads are resumable: each page is saved as it arrives (<id>.partial.json),
so an interrupted run continues where it stopped.

A ROM matches an IGDB game only when their titles are EQUAL after
merge.match_key() (case, punctuation, ™/®, leading "The", & → and) — the same
exact rule the store games use; never fuzzy. Names are tried in this order,
the first that finds anything wins:
  1. the scraped name (es_name), plus the same with "(USA)"-style tags removed
  2. IGDB alternative names (Japanese titles of Variants, "Rockman" …)
  3. the ROM file name, No-Intro style ("Legend of Zelda, The - A Link to the
     Past (USA).zip"); not for MAME, whose file names are short codes
Several exact matches (same-title games): the primary-name match beats an
alternative name, then the game released on that platform closest to the
ROM's release year, then parent games over editions, then the most-rated.

Systems IGDB has no platform for (PICO-8) are looked up by name instead,
`where name = (...)`, 500 per request, limited to NAME_PLATFORMS (PICO-8
carts are listed as browser games; their names are too generic — "Ascent",
"Rolly" — to search PC games).

Fixing a wrong or missing match: rom-igdb-pins.tsv (system, ROM path as in
the gamelist, igdb_id). Pins are applied on every run.

Later runs
----------
Only ROMs never tried (esde_entries.igdb_matched_at IS NULL — new ROMs, or
ROMs whose path changed) are matched. Unmatched ROMs are retried when their
platform list was re-downloaded, or with --retry-failed; --rematch re-checks
every ROM. Browsing details (genres, themes, modes, keywords, series,
companies) are cached per IGDB game in cache/igdb_rom_details.json, and only
games new to the cache, or fetched more than DETAILS_DAYS ago, are requested.
rom_tags is rebuilt from that cache on every run.

Store games
-----------
ROMs and store games with the same igdb_id are linked (esde_entries.game_id_fk,
_db.link_esde_to_games). Remakes, remasters and ports are different IGDB
games (NES "Final Fantasy" vs the Pixel Remaster), so the IGDB relations of
each ROM's game go into igdb_relations, also rebuilt every run; the portal's
Retro / Modern Picks list those first.

Usage:
    ~/game-catalog-venv/bin/python3 match_roms.py [--retry-failed] [--rematch]
                                                  [--refresh-catalogs] [--dry-run]
"""

import argparse, json, pathlib, re, time
from _db import open_db, link_esde_to_games, list_file
from merge import get_token, igdb_query, match_key
from enrich_details import DETAIL_FIELDS, KEYWORD_TO_TAG, tags_for, normalize_tag

CACHE_DIR    = pathlib.Path.home() / ".local/share/game-catalog/cache"
CATALOG_DIR  = CACHE_DIR / "igdb_platforms"
DETAILS_FILE = CACHE_DIR / "igdb_rom_details.json"
PINS_FILE    = list_file("rom-igdb-pins.tsv")

CATALOG_DAYS = 30     # re-download a platform's game list after this many days
DETAILS_DAYS = 30     # re-fetch a game's browsing details after this many days
BATCH        = 500    # IGDB's maximum page size
PAUSE        = 0.26   # IGDB allows 4 requests per second

# ES-DE system folder → IGDB platform ids. Cartridge systems include their
# Japanese twin (Variants hold Japanese releases); gb and gbc include each
# other, since dual-mode cartridges are often listed under one only.
SYSTEMS = {
    "snesna":       [19, 58],         # SNES, Super Famicom
    "sfc":          [58, 19],
    "nes":          [18, 99, 51],     # NES, Famicom, Famicom Disk System
    "famicom":      [99, 18, 51],
    "gb":           [33, 22],         # Game Boy, Game Boy Color
    "gbc":          [22, 33],
    "gba":          [24],
    "nds":          [20, 159],        # DS, DSi
    "n64":          [4],
    "gc":           [21],
    "genesis":      [29],             # Mega Drive/Genesis
    "mastersystem": [64],
    "gamegear":     [35],
    "segacd":       [78],
    "psx":          [7],
    "ps2":          [8],
    "psp":          [38],
    "atari2600":    [59],
    "apple2":       [75],
    "mame":         [52],             # Arcade
}
# Systems without an IGDB platform: matched by a name query on these platforms
NAME_PLATFORMS = {
    "pico8": [82],                    # Web browser
}
# File names that aren't titles
NO_FILE_NAMES = {"mame"}

CATALOG_FIELDS = ("id,name,alternative_names.name,first_release_date,"
                  "release_dates.y,release_dates.platform,game_type.type,"
                  "version_parent,total_rating_count")
# kinds written to rom_tags (raw keywords too: recommend.py uses them)
ROM_TAG_KINDS = ("genre", "theme", "mode", "perspective", "tag", "keyword",
                 "series", "developer", "publisher")
# Detail fields: the store games' browsing fields plus the relations below
ROM_DETAIL_FIELDS = DETAIL_FIELDS + ",remakes,remasters,ports,version_parent"
DETAILS_VERSION   = 2      # bump when ROM_DETAIL_FIELDS changes: cached entries are re-fetched
# IGDB field → relation stored in igdb_relations (related game is a … of the ROM's game)
RELATIONS = {"remakes": "remake", "remasters": "remaster", "ports": "port"}


# ── IGDB platform game lists (cached, resumable) ─────────────────────────────

def load_catalog(token, pid, refresh=False):
    """
    All IGDB games on platform `pid` → (games, downloaded_now). Paged by id
    (id > last) rather than offset, saving each page so a broken download
    resumes instead of starting over.
    """
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    done, part = CATALOG_DIR / f"{pid}.json", CATALOG_DIR / f"{pid}.partial.json"
    if done.exists() and not refresh:
        data = json.loads(done.read_text())
        if time.time() - data["fetched_at"] < CATALOG_DAYS * 86400:
            return data["games"], False
    games = json.loads(part.read_text())["games"] if part.exists() else []
    last = max((g["id"] for g in games), default=0)
    if games:
        print(f"  [roms] platform {pid}: resuming after {len(games)} games")
    while True:
        page = igdb_query(token, "games",
                          f"fields {CATALOG_FIELDS}; where platforms = ({pid}) & id > {last};"
                          f" sort id asc; limit {BATCH};")
        time.sleep(PAUSE)
        if not page:
            break
        games += page
        last = page[-1]["id"]
        part.write_text(json.dumps({"games": games}))
        if len(page) < BATCH:
            break
    done.write_text(json.dumps({"fetched_at": time.time(), "games": games}))
    part.unlink(missing_ok=True)
    print(f"  [roms] platform {pid}: downloaded {len(games)} IGDB games")
    return games, True


def fetch_by_names(token, pids, titles):
    """
    IGDB games on `pids` whose name is exactly one of `titles`. IGDB compares
    case-sensitively, so each title is also sent in Title Case.
    """
    names = sorted({v for t in titles for v in (t, t.title()) if t})
    found = {}
    for i in range(0, len(names), BATCH // 2):        # ≤ 2 spellings per title → ≤ 500 hits
        chunk = ",".join('"%s"' % n.replace('"', "").replace("\\", "") for n in names[i:i + BATCH // 2])
        for g in igdb_query(token, "games",
                            f"fields {CATALOG_FIELDS}; where name = ({chunk})"
                            f" & platforms = ({','.join(map(str, pids))}); limit {BATCH};"):
            found[g["id"]] = g
        time.sleep(PAUSE)
    return list(found.values())


# ── matching ──────────────────────────────────────────────────────────────────

def strip_tags(name):
    """"Final Fantasy XII (USA)" → "Final Fantasy XII"; drops (…) and […] groups."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).strip()


def unflip_article(name):
    """No-Intro "Legend of Zelda, The - A Link…" → "Legend of Zelda - A Link…"
    (match_key drops a leading "The" anyway, so the article can simply go)."""
    return re.sub(r",\s*(The|A|An)\b(?=\s*(-|:|$))", "", name)


def file_title(rom_path):
    """Title from the gamelist path: the file name, or for multi-disc folders the folder."""
    parts = rom_path.removeprefix("./").split("/")
    name = parts[-1].rsplit(".", 1)[0]
    if len(parts) > 1 and parts[0] not in ("Variants", "Translations and Hacks", "Imgs.off"):
        name = parts[0]                          # "Final Fantasy IX (USA)/… (Disc 1).chd"
    return unflip_article(strip_tags(name))


def name_keys(rom, system):
    """[(tier, {keys})]: scraped name first, then file name."""
    es = rom["es_name"]
    tiers = [{match_key(unflip_article(es)), match_key(unflip_article(strip_tags(es))),
              match_key(es.replace("_", " "))}]
    if system not in NO_FILE_NAMES:
        tiers.append({match_key(file_title(rom["rom_path"]))})
    return [t - {""} for t in tiers]


def build_index(games, pids):
    """match_key → [(game, is_alt_name, platform year)] over one system's IGDB games."""
    index = {}
    for g in games:
        years = [r["y"] for r in g.get("release_dates", []) if r.get("platform") in pids and r.get("y")]
        year = min(years) if years else (time.gmtime(g["first_release_date"]).tm_year
                                         if g.get("first_release_date") else None)
        index.setdefault(match_key(g.get("name", "")), []).append((g, False, year))
        for alt in g.get("alternative_names", []):
            k = match_key(alt.get("name", ""))
            if k and k != match_key(g.get("name", "")):
                index.setdefault(k, []).append((g, True, year))
    return index


def rom_year(release_date):
    return int(release_date[:4]) if release_date and release_date[:4].isdigit() else None


def pick(rom, system, index):
    """Best exact match for one ROM, or None. Returns (game, how) — how is for the log."""
    year = rom_year(rom["release_date"])
    for tier, keys in enumerate(name_keys(rom, system)):
        hits = {}
        for k in keys:
            for g, alt, gyear in index.get(k, ()):
                # keep the primary-name hit if a game matches both ways
                if g["id"] not in hits or not alt:
                    hits[g["id"]] = (g, alt, gyear)
        if not hits:
            continue

        def rank(h):
            g, alt, gyear = h
            dist = abs(gyear - year) if (year and gyear) else 99
            return (alt, dist, g.get("version_parent") is not None, -(g.get("total_rating_count") or 0))

        best = min(hits.values(), key=rank)
        how = ("name" if tier == 0 else "file name") + (" (alternative name)" if best[1] else "")
        return best[0], how, len(hits)
    return None, None, 0


def load_pins():
    """rom-igdb-pins.tsv → {(system, rom_path): igdb_id}."""
    pins = {}
    if not PINS_FILE.exists():
        return pins
    for lineno, line in enumerate(PINS_FILE.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#") or line.startswith("system\t"):
            continue
        cols = line.split("\t")
        if len(cols) < 3 or not cols[2].strip().isdigit():
            print(f"  [roms] rom-igdb-pins.tsv:{lineno}: expected system<TAB>rom_path<TAB>igdb_id — skipping")
            continue
        pins[(cols[0].strip(), cols[1].strip())] = int(cols[2])
    return pins


# ── browsing details (cached per IGDB game) ───────────────────────────────────

def refresh_details(token, igdb_ids):
    """Fetch DETAIL_FIELDS for ids missing from the cache or stale; return the cache."""
    cache = json.loads(DETAILS_FILE.read_text()) if DETAILS_FILE.exists() else {}
    cutoff = time.time() - DETAILS_DAYS * 86400
    todo = sorted(i for i in igdb_ids
                  if cache.get(str(i), {}).get("_at", 0) < cutoff
                  or cache.get(str(i), {}).get("_v") != DETAILS_VERSION)
    for n in range(0, len(todo), BATCH):
        ids = todo[n:n + BATCH]
        got = {r["id"]: r for r in igdb_query(
            token, "games", f"fields {ROM_DETAIL_FIELDS}; where id = ({','.join(map(str, ids))}); limit {BATCH};")}
        time.sleep(PAUSE)
        for i in ids:                       # store misses too, so they aren't re-asked every run
            cache[str(i)] = {**got.get(i, {}), "_at": time.time(), "_v": DETAILS_VERSION}
        DETAILS_FILE.write_text(json.dumps(cache))
        print(f"  [roms] details fetched {min(n + BATCH, len(todo))}/{len(todo)}")
    return cache


def rom_tag_rows(db, cache):
    """(rom_id, kind, value) rows for every matched ROM, normalized like game_tags."""
    unmapped, rows = set(), set()
    for rom_id, igdb_id in db.execute("SELECT id, igdb_id FROM esde_entries WHERE igdb_id IS NOT NULL"):
        details = cache.get(str(igdb_id))
        if not details:
            continue
        for kind, value in tags_for(details):
            v = normalize_tag(kind, value, unmapped)
            if v and kind in ROM_TAG_KINDS:
                rows.add((rom_id, kind, v))
            if kind == "keyword" and v in KEYWORD_TO_TAG:
                rows.add((rom_id, "tag", KEYWORD_TO_TAG[v]))
    if unmapped:
        print(f"  [roms] genres not in GENRE_MAP (kept as-is): {sorted(unmapped)}")
    return rows


def relation_rows(igdb_ids, cache):
    """(igdb_id, related_igdb_id, relation) for every matched ROM game. version_parent
    points the other way (this game is an edition OF that one), stored as "original"."""
    rows = set()
    for i in igdb_ids:
        d = cache.get(str(i), {})
        for field, relation in RELATIONS.items():
            rows.update((i, other, relation) for other in d.get(field, []))
        if d.get("version_parent"):
            rows.add((i, d["version_parent"], "original"))
    return rows


# ── main ──────────────────────────────────────────────────────────────────────

def run(db, retry_failed=False, rematch=False, refresh_catalogs=False, dry_run=False):
    token = get_token()
    pins = load_pins()
    roms = db.execute("SELECT id, system, rom_path, es_name, release_date, igdb_id, igdb_matched_at "
                      "FROM esde_entries ORDER BY system, id").fetchall()
    systems = sorted({r["system"] for r in roms})
    stats = {}
    updates = []                         # (igdb_id, rom_id)

    for system in systems:
        mine = [r for r in roms if r["system"] == system]
        pids = SYSTEMS.get(system)
        refreshed = False
        if pids:
            games = []
            for pid in pids:
                g, new = load_catalog(token, pid, refresh_catalogs)
                games += g
                refreshed |= new
        elif system in NAME_PLATFORMS:
            pids = NAME_PLATFORMS[system]
            todo_names = [r["es_name"] for r in mine if rematch or r["igdb_matched_at"] is None
                          or (retry_failed and r["igdb_id"] is None)]
            games = fetch_by_names(token, pids, {strip_tags(n) for n in todo_names}) if todo_names else []
        else:
            print(f"  [roms/{system}] no IGDB platform known — skipped (add it to SYSTEMS)")
            stats[system] = None
            continue

        index = build_index(games, set(pids))
        tried = collisions = 0
        for r in mine:
            pinned = pins.get((system, r["rom_path"]))
            if pinned:
                if pinned != r["igdb_id"]:
                    updates.append((pinned, r["id"]))
                continue
            retry = r["igdb_id"] is None and (retry_failed or refreshed)
            if not (rematch or r["igdb_matched_at"] is None or retry):
                continue
            tried += 1
            game, how, n = pick(r, system, index)
            if n > 1:
                collisions += 1
            new_id = game["id"] if game else None
            if rematch and r["igdb_id"] and new_id != r["igdb_id"]:
                print(f"  [roms/{system}] rematch {r['es_name']!r}: {r['igdb_id']} → {new_id}")
            updates.append((new_id, r["id"]))
        print(f"  [roms/{system}] {len(mine)} ROMs, {tried} looked up"
              f"{', ' + str(collisions) + ' same-title collisions' if collisions else ''}"
              f"{' (platform list re-downloaded)' if refreshed else ''}")
        stats[system] = len(mine)

    if dry_run:
        print(f"[roms] dry run: {sum(1 for u in updates if u[0])} of {len(updates)} looked-up ROMs would match")
        return

    db.executemany("UPDATE esde_entries SET igdb_id = ?, "
                   "igdb_matched_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", updates)
    db.commit()

    # Browsing details for every matched game, then rom_tags from the cache
    ids = [r[0] for r in db.execute("SELECT DISTINCT igdb_id FROM esde_entries WHERE igdb_id IS NOT NULL")]
    cache = refresh_details(token, ids)
    rows = rom_tag_rows(db, cache)
    db.execute("DELETE FROM rom_tags")
    db.executemany("INSERT INTO rom_tags (rom_id, kind, value) VALUES (?, ?, ?)", sorted(rows))
    relations = relation_rows(ids, cache)
    db.execute("DELETE FROM igdb_relations")
    db.executemany("INSERT INTO igdb_relations (igdb_id, related_igdb_id, relation) VALUES (?, ?, ?)",
                   sorted(relations))
    linked = link_esde_to_games(db)
    db.commit()
    related = db.execute("""
        SELECT COUNT(DISTINCT e.id) FROM esde_entries e
        JOIN igdb_relations r ON r.igdb_id = e.igdb_id
        JOIN games g ON g.igdb_id = r.related_igdb_id""").fetchone()[0]

    print("[roms] match rate per system:")
    total = matched = 0
    for system, n in stats.items():
        if n is None:
            continue
        m = db.execute("SELECT COUNT(*) FROM esde_entries WHERE system = ? AND igdb_id IS NOT NULL",
                       (system,)).fetchone()[0]
        total, matched = total + n, matched + m
        print(f"  {system:13s} {m:5d} / {n:5d}  {100 * m / max(n, 1):5.1f}%")
    print(f"[roms] done: {matched} / {total} ROMs matched ({100 * matched / max(total, 1):.1f}%), "
          f"{len(ids)} distinct IGDB games, {len(rows)} tags, {linked} ROMs linked to store games "
          f"(same game), {related} more with a remake/remaster/port in the store library")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Match ES-DE ROMs to IGDB and fill rom_tags")
    ap.add_argument("--retry-failed", action="store_true", help="Retry ROMs that found no match before")
    ap.add_argument("--rematch", action="store_true", help="Re-check every ROM, matched or not")
    ap.add_argument("--refresh-catalogs", action="store_true",
                    help=f"Re-download IGDB platform game lists now (default: every {CATALOG_DAYS} days)")
    ap.add_argument("--dry-run", action="store_true", help="Match but do not write to the DB")
    a = ap.parse_args()
    with open_db() as db:
        run(db, retry_failed=a.retry_failed, rematch=a.rematch,
            refresh_catalogs=a.refresh_catalogs, dry_run=a.dry_run)

#!/usr/bin/env python3
"""
Browsing metadata pass — fills the game_tags table and the rating /
time-to-beat columns on games. Run after merge.py (needs games.igdb_id).

For every IGDB-matched game, fetches by id (500 per request, so ~3 requests
for the whole library):
  genres, themes, game modes, player perspectives, keywords,
  franchises + collections (→ "series"), developers, publishers,
  total_rating / total_rating_count
and from the game_time_to_beats endpoint: hastily / normally / completely.
Keywords are also grouped into a curated set of playstyle tags (TAG_GROUPS,
stored as kind='tag'), since raw IGDB keywords are mostly noise.

Unmatched games only get genres, taken from games.genres_json (store data,
mostly Game Pass categories).

Genres come from two vocabularies — IGDB ("Role-playing (RPG)", "Simulator",
"Platform") and Game Pass ("Role playing", "Simulation", "Platformer") — so
every genre goes through GENRE_MAP into one list. A genre missing from the
map is kept as-is and printed, so new ones get noticed and mapped.

game_tags is WIPED and rebuilt on every run (derived data; nothing manual
lives there). IGDB is queried first and the DB is only touched if every
request succeeded, so a network failure leaves the old tags in place.

Usage:
    ~/game-catalog-venv/bin/python3 enrich_details.py [--dry-run]
"""

import argparse, json, time
from _db import open_db
from merge import get_token, igdb_query

BATCH = 500   # IGDB's maximum page size

# source genre name → normalized genre (None = drop: not really a genre)
GENRE_MAP = {
    "Adventure":                   "Adventure",
    "Action & adventure":          "Adventure",
    "Indie":                       "Indie",
    "Role-playing (RPG)":          "RPG",
    "Role playing":                "RPG",
    "Strategy":                    "Strategy",
    "Turn-based strategy (TBS)":   "Turn-based strategy",
    "Real Time Strategy (RTS)":    "Real-time strategy",
    "Tactical":                    "Tactics",
    "Simulator":                   "Simulation",
    "Simulation":                  "Simulation",
    "Shooter":                     "Shooter",
    "Puzzle":                      "Puzzle",
    "Puzzle & trivia":             "Puzzle",
    "Platform":                    "Platformer",
    "Platformer":                  "Platformer",
    "Arcade":                      "Arcade",
    "Hack and slash/Beat 'em up":  "Beat 'em up / Hack and slash",
    "Point-and-click":             "Point-and-click",
    "Fighting":                    "Fighting",
    "Card & Board Game":           "Card & board",
    "Card & board":                "Card & board",
    "Racing":                      "Racing",
    "Racing & flying":             "Racing",
    "Sport":                       "Sports",
    "Sports":                      "Sports",
    "Visual Novel":                "Visual novel",
    "Music":                       "Music / rhythm",
    "MOBA":                        "MOBA",
    "Multi-player Online Battle Arena": "MOBA",
    "Quiz/Trivia":                 "Quiz / trivia",
    "Pinball":                     "Pinball",
    # Game Pass catch-alls that aren't genres
    "Family & kids":               None,   # IGDB's "Kids" theme covers matched games
    "Classics":                    None,
    "Other":                       None,
}

# Curated playstyle tags built from IGDB keywords. Raw keywords are mostly
# noise for browsing ("steam achievements", "pax west 2017", "chicken"), so
# only these groups become kind='tag' (the portal's "Tags" filter). Picked
# from keywords that appear on 12+ of this library's games. Add groups or
# variants freely; the raw keywords stay in game_tags as kind='keyword'.
TAG_GROUPS = {
    "Roguelike":            ["roguelike", "roguelite"],
    "Metroidvania":         ["metroidvania"],
    "Soulslike":            ["soulslike", "souls-like"],
    "Story rich":           ["story rich", "story driven", "narrative-driven"],
    "Choices matter":       ["choices matter", "multiple endings", "branching storyline", "moral decisions"],
    "Turn-based combat":    ["turn-based", "turn-based combat", "tactical turn-based combat", "turn-based rpg"],
    "Party-based":          ["party system"],
    "Dungeon crawler":      ["dungeon crawler"],
    "Crafting":             ["crafting"],
    "Base building":        ["base building"],
    "City builder":         ["city builder", "colony"],
    "Management":           ["management", "resource management", "micromanagement"],
    "Procedurally generated": ["procedural generation", "randomly generated maps"],
    "Permadeath":           ["permadeath"],
    "Difficult":            ["difficult"],
    "Relaxing":             ["relaxing", "casual"],
    "Puzzle platformer":    ["puzzle platformer"],
    "3D platformer":        ["3d platformer"],
    "Shoot 'em up":         ["shoot 'em up", "bullet hell"],
    "Twin-stick":           ["twin stick control"],
    "Tower defense":        ["tower defense"],
    "Survival horror":      ["survival horror", "psychological horror"],
    "Walking simulator":    ["walking simulator"],
    "Detective":            ["detective"],
    "Pixel art":            ["pixel art", "pixel graphics"],
    "Hand-drawn":           ["hand-drawn"],
    "Anime":                ["anime"],
    "Cyberpunk":            ["cyberpunk"],
    "Post-apocalyptic":     ["post-apocalyptic"],
    "Steampunk":            ["steampunk"],
    "Space":                ["space", "spaceship"],
    "Zombies":              ["zombies"],
    "Medieval":             ["medieval"],
    "Dark fantasy":         ["dark fantasy"],
    "Time travel":          ["time travel", "time manipulation"],
    "World War II":         ["world war ii - ww2"],
    "Female protagonist":   ["female protagonist"],
    "LGBTQ+":               ["lgbtq+"],
    "Mod support":          ["mod support", "steam workshop", "user generated content"],
    "Free-to-play":         ["free-to-play"],
}
KEYWORD_TO_TAG = {kw: tag for tag, kws in TAG_GROUPS.items() for kw in kws}

# Shorter display names for a few IGDB labels
RENAME = {
    ("theme", "4X (explore, expand, exploit, and exterminate)"): "4X",
    ("mode", "Massively Multiplayer Online (MMO)"):              "MMO",
    ("perspective", "Bird view / Isometric"):                    "Isometric / top-down",
}

DETAIL_FIELDS = (
    "genres.name,themes.name,game_modes.name,player_perspectives.name,"
    "keywords.name,franchises.name,collections.name,"
    "involved_companies.company.name,involved_companies.developer,"
    "involved_companies.publisher,total_rating,total_rating_count"
)


def _names(record, field):
    return [x["name"] for x in record.get(field, []) if x.get("name")]


def fetch_igdb(token, igdb_ids):
    """Return ({igdb_id: details}, {igdb_id: ttb}). Raises on any request failure."""
    details, ttb = {}, {}
    for i in range(0, len(igdb_ids), BATCH):
        ids = ",".join(map(str, igdb_ids[i:i + BATCH]))
        for r in igdb_query(token, "games",
                            f"fields {DETAIL_FIELDS}; where id = ({ids}); limit {BATCH};"):
            details[r["id"]] = r
        time.sleep(0.26)
        for r in igdb_query(token, "game_time_to_beats",
                            f"fields game_id,hastily,normally,completely;"
                            f" where game_id = ({ids}); limit {BATCH};"):
            ttb[r["game_id"]] = r
        time.sleep(0.26)
        print(f"  [details] fetched {min(i + BATCH, len(igdb_ids))}/{len(igdb_ids)}")
    return details, ttb


def tags_for(details):
    """Yield (kind, value) pairs for one IGDB game record."""
    for g in _names(details, "genres"):
        yield "genre", g
    for kind, field in (("theme", "themes"), ("mode", "game_modes"),
                        ("perspective", "player_perspectives"), ("keyword", "keywords"),
                        ("series", "franchises"), ("series", "collections")):
        for v in _names(details, field):
            yield kind, v
    for ic in details.get("involved_companies", []):
        name = (ic.get("company") or {}).get("name")
        if not name:
            continue
        if ic.get("developer"):
            yield "developer", name
        if ic.get("publisher"):
            yield "publisher", name


def normalize_tag(kind, value, unmapped):
    """Map a raw (kind, value) to its display form; None means drop it."""
    if kind == "genre":
        if value not in GENRE_MAP:
            unmapped.add(value)
            return value
        return GENRE_MAP[value]
    if kind == "keyword":
        return value.strip().lower()   # IGDB keyword casing is inconsistent
    return RENAME.get((kind, value), value)


def _hours(seconds):
    return round(seconds / 3600, 1) if seconds else None


def run(db, dry_run=False):
    games = db.execute("""
        SELECT id, igdb_id, genres_json FROM games
        WHERE id IN (SELECT game_id FROM ownership WHERE kind != 'launcher')
    """).fetchall()
    matched = [g for g in games if g["igdb_id"]]
    print(f"[details] {len(games)} games, {len(matched)} IGDB-matched")

    details, ttb = fetch_igdb(get_token(), [g["igdb_id"] for g in matched])

    unmapped = set()
    rows = set()   # (game_id, kind, value) — a set, since series often repeats
    for g in games:
        if g["igdb_id"] and g["igdb_id"] in details:
            raw = tags_for(details[g["igdb_id"]])
        else:
            raw = (("genre", x) for x in json.loads(g["genres_json"] or "[]"))
        for kind, value in raw:
            v = normalize_tag(kind, value, unmapped)
            if v:
                rows.add((g["id"], kind, v))
            if kind == "keyword" and v in KEYWORD_TO_TAG:
                rows.add((g["id"], "tag", KEYWORD_TO_TAG[v]))

    if unmapped:
        print(f"  [details] genres not in GENRE_MAP (kept as-is): {sorted(unmapped)}")

    if dry_run:
        print(f"[details] dry run: would write {len(rows)} tags")
        return

    db.execute("DELETE FROM game_tags")
    db.executemany("INSERT INTO game_tags (game_id, kind, value) VALUES (?, ?, ?)", sorted(rows))
    for g in matched:
        d = details.get(g["igdb_id"], {})
        t = ttb.get(g["igdb_id"], {})
        db.execute("""
            UPDATE games SET
                rating               = ?,
                rating_count         = ?,
                ttb_hastily_hours    = ?,
                ttb_normally_hours   = ?,
                ttb_completely_hours = ?,
                details_fetched_at   = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE id = ?
        """, (round(d["total_rating"], 1) if d.get("total_rating") else None,
              d.get("total_rating_count"),
              _hours(t.get("hastily")), _hours(t.get("normally")), _hours(t.get("completely")),
              g["id"]))
    db.commit()

    for kind, n, games_with in db.execute("""
            SELECT kind, COUNT(DISTINCT value), COUNT(DISTINCT game_id)
            FROM game_tags GROUP BY kind ORDER BY kind"""):
        print(f"  [details] {kind:12s} {n:5d} distinct values on {games_with} games")
    print(f"[details] done: {len(rows)} tags, "
          f"{sum(1 for g in matched if g['igdb_id'] in ttb)} games with time-to-beat")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fill game_tags, ratings and time-to-beat from IGDB")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write to the DB")
    args = parser.parse_args()
    with open_db() as db:
        run(db, dry_run=args.dry_run)

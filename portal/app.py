#!/usr/bin/env python3
"""
Game catalog web portal — http://ebonhawk.local:5000

Usage (on bazzite):
    ~/game-catalog-venv/bin/python3 portal/app.py

For auto-start see game-catalog-portal.service in the parent directory.

Pages
-----
  /                home: Play next, picks by device / itch (home.py)
  /games           store games grid (this file)     /game/<id>   game detail
  /roms            ES-DE ROMs grid (roms.py)         /rom/<id>    ROM detail
  /scans           scan health + rescan buttons (scans.py)

Browsing model
--------------
The library is small (~1,500 games), so the grid loads every game plus its
browsable tags once (cached CACHE_SECS) and filters in Python instead of
building SQL per request. The filter/count/sort/URL mechanics are shared
with the ROM grid in browse.py, e.g.
    /games?genre=RPG&genre=Strategy&mode=Co-operative&sort=rating
(The grid was at "/" until 2026-11; home.py redirects old links.)
series / developer / publisher work like sidebar filters but have no
sidebar section; the game detail page links to them.
"""

import json, pathlib, sqlite3, time
from flask import Flask, render_template, request, g, abort, jsonify

import browse
import recommend
import roms
import settings
import mine
import home
from roms import make_blueprint as make_roms_blueprint
from scans import make_blueprint as make_scans_blueprint

DB_PATH = pathlib.Path.home() / ".local/share/game-catalog/library.db"

app = Flask(__name__)

STORE_LABELS = {
    "steam":    "Steam",
    "epic":     "Epic",
    "gog":      "GOG",
    "amazon":   "Prime",
    "gamepass": "Game Pass",
    "lutris":   "Lutris",
    "battlenet": "Battle.net",
    "ea":       "EA App",
    "ubisoft":  "Ubisoft",
}

OWN_LABELS = {
    "purchased":    "Bought",
    "free_claim":   "Claimed free",
    "subscription": "Subscription",
    "library":      "In account library",
}

COMPAT_ORDER = ["works", "partial", "broken", "untested"]

# "Game Pass on" facet, from ownership.platforms of gamepass rows.
# "Cloud streamable" = playable through the Xbox app's cloud gaming (any
# device, the Odin 2 included); "Windows only" = not on the cloud list.
# This does NOT predict DroidDeck, which installs PC packages locally:
# checked on the Odin 2 (2026-10-22), Call of Duty ("Windows only") is
# installable there, while Diablo IV (Battle.net) and Anno 1800 (Ubisoft
# Connect) aren't in DroidDeck's Xbox catalog at all.
GAMEPASS_ACCESS = ["Cloud streamable", "Windows only", "Xbox console"]

# "DroidDeck (Odin 2)" facet, from droiddeck_status (tools/droiddeck_import.py):
# checked results from the scanner on the Odin 2, or predictions from the Xodus
# package check for Game Pass titles added since the last scan.
DROIDDECK_LABELS = ["Installable", "Likely installable", "Not available", "Unclear"]


def _droiddeck(statuses):
    """GROUP_CONCAT of "status:source" for a game's gamepass rows → facet values."""
    out = set()
    for item in (statuses or "").split(","):
        status, _, source = item.partition(":")
        if status == "installable":
            out.add("Likely installable" if source == "predicted" else "Installable")
        elif status in ("missing", "findable"):
            out.add("Not available")
        elif status:
            out.add("Unclear")
    if {"Installable", "Likely installable"} & out:     # one installable edition is enough
        out -= {"Not available", "Unclear"}
    return out


# "Bazzite via Lutris" facet and game-page note: games that play on bazzite
# through the Battle.net / EA App / Ubisoft Connect launchers set up in Lutris.
# That's everything in those account libraries (stores battlenet / ea /
# ubisoft, importers/lutris.py), and Game Pass titles whose publisher ships
# them through its own launcher — Game Pass PC hands Ubisoft games to Ubisoft
# Connect and Blizzard games to Battle.net (DroidDeck can't install those).
# Not Activision: Call of Duty on Game Pass runs from the Xbox app (its
# Battle.net copies come in through the battlenet store instead).
LUTRIS_LAUNCHERS = {"battlenet": "Battle.net", "ea": "EA App", "ubisoft": "Ubisoft Connect"}
GP_PUBLISHER_LAUNCHERS = [("ubisoft", "Ubisoft Connect"), ("blizzard", "Battle.net"),
                          ("electronic arts", "EA App")]


def _lutris_via(stores, gp_publishers):
    """Launcher names a game plays through on bazzite, sorted."""
    via = {LUTRIS_LAUNCHERS[s] for s in stores if s in LUTRIS_LAUNCHERS}
    for pub in (gp_publishers or "").lower().split("|"):
        via |= {launcher for word, launcher in GP_PUBLISHER_LAUNCHERS if word in pub}
    return sorted(via)


def _gamepass_access(platforms):
    """Comma-joined platforms of a game's gamepass rows → facet values."""
    tokens = set((platforms or "").replace(",", " ").split())
    if not tokens:
        return set()
    access = {"Cloud streamable" if "cloud" in tokens else "Windows only"}
    if "console" in tokens:
        access.add("Xbox console")
    return access

# (upper bound in hours, label); None = no upper bound
LENGTH_BUCKETS = [
    (5,    "Under 5 hours"),
    (15,   "5–15 hours"),
    (40,   "15–40 hours"),
    (None, "40+ hours"),
]

# Sidebar facets, in display order: (url param, heading, open by default)
FACETS = [
    ("genre",       "Genre",          True),
    ("theme",       "Theme",          True),
    ("mode",        "Game mode",      True),
    ("tag",         "Tags",           True),
    ("store",       "Store",          True),
    ("gp",          "Game Pass on",   True),
    ("droid",       "DroidDeck (Odin 2)", True),
    ("lutris",      "Bazzite via Lutris", True),
    ("own",         "How you own it", False),
    ("perspective", "Perspective",    False),
    ("length",      "Length",         False),
    ("decade",      "Decade",         False),
    ("compat",      "Odin 2",         False),
    ("mystatus",    "My status",      False),
    ("myrating",    "My rating",      False),
]
# Filters reachable only by link (detail page → "more by this developer"):
# hundreds of values each, so they get chips but no sidebar section.
LINK_FILTERS = [
    ("series",    "Series"),
    ("developer", "Developer"),
    ("publisher", "Publisher"),
]
FACET_KEYS  = [f[0] for f in FACETS]
FILTER_KEYS = FACET_KEYS + [k for k, _ in LINK_FILTERS]
HEADINGS    = {k: h for k, h, _ in FACETS} | dict(LINK_FILTERS)
# game_tags kinds loaded into the catalog (everything except raw keywords)
TAG_KINDS   = ("genre", "theme", "mode", "perspective", "tag", "series", "developer", "publisher")

SORTS = {
    "title":    "A–Z",
    "newest":   "Newest",
    "oldest":   "Oldest",
    "rating":   "Top rated",
    "shortest": "Shortest",
    "longest":  "Longest",
    "recent":   "Recently played",
    "played":   "Most played",
    "myrating": "My rating",
}

# Detail page: tag rows shown (kind, heading), in order — each value links to /?kind=value
DETAIL_TAG_ROWS = [
    ("genre",       "Genre"),
    ("theme",       "Theme"),
    ("mode",        "Modes"),
    ("perspective", "Perspective"),
    ("tag",         "Tags"),
]
TTB_ROWS = [
    ("ttb_hastily_hours",    "Main story"),
    ("ttb_normally_hours",   "Main + extras"),
    ("ttb_completely_hours", "Completionist"),
]
SERIES_MAX = 18   # games shown per "same series" row

MIN_RATINGS    = 5     # rating sort ignores ratings from fewer reviews
CACHE_SECS     = 60


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        if not DB_PATH.exists():
            abort(503, "Database not found — run refresh.sh first")
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db:
        db.close()


# ── catalog (cached) ──────────────────────────────────────────────────────────

_cache = {"at": 0.0, "games": None}


def _length_bucket(hours):
    if not hours:
        return None
    for limit, label in LENGTH_BUCKETS:
        if limit is None or hours < limit:
            return label


def _load_catalog(db):
    games = {}
    for r in db.execute("""
        SELECT g.id, g.igdb_id, g.canonical_title, g.first_release_year, g.cover_url,
               g.rating, g.rating_count,
               COALESCE(g.ttb_normally_hours, g.ttb_hastily_hours) AS ttb,
               GROUP_CONCAT(DISTINCT o.store) AS stores,
               GROUP_CONCAT(DISTINCT o.kind)  AS kinds,
               GROUP_CONCAT(DISTINCT o.store || ':' || o.store_id) AS refs,
               MAX(o.art_cover_url)           AS store_cover,
               GROUP_CONCAT(CASE WHEN o.store = 'gamepass' THEN o.platforms END) AS gp_platforms,
               GROUP_CONCAT(CASE WHEN o.store = 'gamepass'
                                 THEN json_extract(o.raw_json, '$.publisher') END, '|') AS gp_publishers,
               (SELECT GROUP_CONCAT(d.status || ':' || d.source)
                  FROM ownership o2 JOIN droiddeck_status d ON d.store_id = o2.store_id
                 WHERE o2.game_id = g.id AND o2.store = 'gamepass') AS droiddeck,
               (SELECT status FROM compat_notes cn
                 WHERE cn.game_id = g.id AND cn.device = 'odin2')  AS compat,
               (SELECT MAX(last_played_at) FROM playtime p WHERE p.game_id = g.id) AS last_played,
               (SELECT SUM(minutes_total)  FROM playtime p WHERE p.game_id = g.id) AS minutes
        FROM games g
        JOIN ownership o ON o.game_id = g.id
        WHERE o.kind != 'launcher'
        GROUP BY g.id
    """):
        year   = r["first_release_year"]
        compat = r["compat"] or "untested"
        games[r["id"]] = {
            "id":          r["id"],
            "kind":        "game",                                             # vs "rom" (home page rows)
            "igdb_id":     r["igdb_id"],
            "title":       r["canonical_title"],
            "title_lower": r["canonical_title"].lower(),
            "search":      browse.search_text(r["canonical_title"]),
            "year":        year,
            "cover":       r["cover_url"] or r["store_cover"],
            "stores":      [s for s in STORE_LABELS if s in (r["stores"] or "").split(",")],
            "refs":        (r["refs"] or "").split(",") if r["refs"] else [],   # my_games keys
            "my":          None,                                               # mine.annotate()
            "rating":      r["rating"],
            "rating_count": r["rating_count"] or 0,
            "ttb":         r["ttb"],
            "last_played": r["last_played"],
            "minutes":     r["minutes"] or 0,
            "compat":      compat,
            # facet → set of values this game has
            "f": {
                "store":  set((r["stores"] or "").split(",")) - {""},
                "gp":     _gamepass_access(r["gp_platforms"]),
                "droid":  _droiddeck(r["droiddeck"]),
                "lutris": set(_lutris_via((r["stores"] or "").split(","), r["gp_publishers"])),
                "own":    set((r["kinds"] or "").split(",")) - {"", "launcher"},
                "decade": {f"{year // 10 * 10}s"} if year else set(),
                "length": {_length_bucket(r["ttb"])} - {None},
                "compat": {compat},
                **{k: set() for k in TAG_KINDS},
            },
        }
    for r in db.execute(f"SELECT game_id, kind, value FROM game_tags "
                        f"WHERE kind IN ({','.join('?' * len(TAG_KINDS))})", TAG_KINDS):
        if r["game_id"] in games:
            games[r["game_id"]]["f"][r["kind"]].add(r["value"])

    # Traits for "Tangents" (themes and non-noise keywords)
    raw = {}
    kinds = recommend.TRAIT_KINDS
    for r in db.execute(f"SELECT game_id, kind, value FROM game_tags "
                        f"WHERE kind IN ({','.join('?' * len(kinds))})", kinds):
        raw.setdefault(r["game_id"], []).append((r["kind"], r["value"]))
    keywords = {}                               # raw keywords, for exclusions
    for gid, pairs in raw.items():
        keywords[gid] = {v for kind, v in pairs if kind == "keyword"}
    for gid, game in games.items():
        game["traits"] = recommend.trait_set(raw.get(gid, ()))
        game["labels"] = recommend.item_labels(game["f"], keywords.get(gid, ()))
        # labels for Retro / Modern Picks (the same sets as the facets)
        game["rel"] = recommend.rel_sets(game["f"], game["traits"])
    return list(games.values())


def catalog():
    if _cache["games"] is None or time.time() - _cache["at"] > CACHE_SECS:
        _cache["games"] = _load_catalog(get_db())
        _cache["by_id"] = {gm["id"]: gm for gm in _cache["games"]}
        _cache["by_igdb"] = {gm["igdb_id"]: gm for gm in _cache["games"] if gm["igdb_id"]}
        _cache["weights"], _cache["n"] = recommend.build_weights(_cache["games"])
        _cache["rel_weights"] = recommend.build_rel_weights(_cache["games"])
        _cache["at"] = time.time()
    return _cache["games"]


def games_data():
    """The loaded store-game cache, for the ROM pages' Modern Picks."""
    catalog()
    return _cache


@app.template_filter("hours")
def fmt_hours(h):
    """4.36 → '4 h', 0.4 → '<1 h', 12.5 → '13 h' (rounded; these are estimates)."""
    if h is None:
        return ""
    return "<1 h" if h < 1 else f"{h:.0f} h"


@app.context_processor
def url_helpers():
    return dict(toggle_url=browse.toggle_url, page_url=browse.page_url,
                current_params=browse.params)


# ── facet labels, option order, sorts ─────────────────────────────────────────

def _label(facet, value):
    if facet == "store":
        return STORE_LABELS.get(value, value)
    if facet == "own":
        return OWN_LABELS.get(value, value)
    if facet == "compat":
        return value.title()
    return value


def _option_order(facet, counts):
    if facet == "store":
        return [s for s in STORE_LABELS if s in counts]
    if facet == "own":
        return [k for k in OWN_LABELS if k in counts]
    if facet == "compat":
        return [c for c in COMPAT_ORDER if c in counts]
    if facet == "droid":
        return [a for a in DROIDDECK_LABELS if a in counts]
    if facet == "mystatus":
        return [s for s in mine.MYSTATUS_LABELS if s in counts]
    if facet == "myrating":
        return [s for s in mine.MYRATING_LABELS if s in counts]
    if facet == "gp":
        return [a for a in GAMEPASS_ACCESS if a in counts]
    if facet == "length":
        return [label for _, label in LENGTH_BUCKETS if label in counts]
    if facet == "decade":
        return sorted(counts, reverse=True)
    return browse.by_count(counts)


# sort key → (value getter, descending?)
SORT_GETTERS = {
    "newest":   (lambda g: g["year"], True),
    "oldest":   (lambda g: g["year"], False),
    "rating":   (lambda g: g["rating"] if g["rating_count"] >= MIN_RATINGS else None, True),
    "shortest": (lambda g: g["ttb"], False),
    "longest":  (lambda g: g["ttb"], True),
    "recent":   (lambda g: g["last_played"], True),
    "played":   (lambda g: g["minutes"] or None, True),
    "myrating": (lambda g: (g["my"] or {}).get("rating"), True),
}


# ── routes ────────────────────────────────────────────────────────────────────

@app.context_processor
def sidebar_stats():
    """
    Sidebar numbers for base.html. Injected into EVERY template so pages that
    extend base.html don't each have to pass them (the detail page used to 500
    with "'stats' is undefined" because only index() passed them).
    """
    db = get_db()
    stats = {
        s: db.execute(
            "SELECT COUNT(DISTINCT game_id) FROM ownership WHERE store=? AND kind!='launcher'",
            (s,)
        ).fetchone()[0]
        for s in STORE_LABELS
    }
    total_games = db.execute(
        "SELECT COUNT(DISTINCT game_id) FROM ownership WHERE kind!='launcher'"
    ).fetchone()[0]
    igdb_pct = db.execute(
        "SELECT ROUND(100.0 * COUNT(igdb_id) / MAX(COUNT(*), 1)) FROM games"
    ).fetchone()[0] or 0
    return dict(stats=stats, total_games=total_games, igdb_pct=int(igdb_pct),
                store_labels=STORE_LABELS)


@app.route("/games")
def index():
    q    = request.args.get("q", "").strip()
    sort = request.args.get("sort", "title")
    if sort not in SORTS:
        sort = "title"

    selected = browse.selected_filters(FILTER_KEYS)
    base = mine.annotate(catalog(), "game")      # the user's status/rating, per request
    if q:
        base = browse.search(base, q)
    results = browse.sort_items([gm for gm in base if browse.matches(gm, selected)],
                                sort, SORT_GETTERS)
    games, page, total_pages = browse.paginate(results)

    return render_template("index.html",
        games=games, total=len(results), page=page, total_pages=total_pages,
        q=q, sort=sort, sorts=SORTS,
        facets=browse.build_facets(base, selected, FACETS, _label, _option_order),
        chips=browse.chips(selected, FILTER_KEYS, HEADINGS, _label),
        n_active=sum(len(v) for v in selected.values()),
        clear_url=browse.clear_url(),
    )


@app.route("/game/<int:game_id>")
def game_detail(game_id):
    db = get_db()
    g_row = db.execute(
        "SELECT * FROM games WHERE id = ?", (game_id,)
    ).fetchone()
    if not g_row:
        abort(404)

    ownerships = db.execute(
        "SELECT o.*, d.status AS dd_status, d.source AS dd_source, d.checked_at AS dd_checked, "
        "d.offer AS dd_offer FROM ownership o "
        "LEFT JOIN droiddeck_status d ON o.store = 'gamepass' AND d.store_id = o.store_id "
        "WHERE o.game_id = ? ORDER BY o.store", (game_id,)
    ).fetchall()

    compat = db.execute(
        "SELECT * FROM compat_notes WHERE game_id = ? ORDER BY device", (game_id,)
    ).fetchall()

    playtime = db.execute(
        "SELECT source, minutes_total, last_played_at FROM playtime "
        "WHERE game_id = ? AND minutes_total > 0 ORDER BY minutes_total DESC", (game_id,)
    ).fetchall()

    # Tags grouped by kind; each one links to the grid filtered on it
    tags = {}
    for r in db.execute("SELECT kind, value FROM game_tags WHERE game_id = ? AND kind != 'keyword' "
                        "ORDER BY value COLLATE NOCASE", (game_id,)):
        tags.setdefault(r["kind"], []).append(r["value"])
    if "genre" not in tags and g_row["genres_json"]:
        tags["genre"] = json.loads(g_row["genres_json"])     # unmatched games: store genres
    tag_rows = [(key, heading, tags[key]) for key, heading in DETAIL_TAG_ROWS if tags.get(key)]

    # Other owned games in the same series, oldest first; one section per
    # series (IGDB franchises and collections can differ, e.g. "Fallout" vs
    # "Fallout Classics"). Sections that would only repeat another are dropped.
    series_sections, seen = [], set()
    for name in tags.get("series", []):
        members = sorted((gm for gm in catalog()
                          if name in gm["f"]["series"] and gm["id"] != game_id),
                         key=lambda gm: (gm["year"] or 9999, gm["title_lower"]))
        ids = frozenset(gm["id"] for gm in members)
        if members and ids not in seen:
            seen.add(ids)
            series_sections.append({"name": name, "games": members[:SERIES_MAX],
                                    "more": len(members) - SERIES_MAX})

    # IGDB time-to-beat is crowd-submitted and sometimes out of order (Fallout 2:
    # extras 39 h, completionist 15 h). Each tier must be ≥ the one before it;
    # drop any that isn't rather than show an impossible number.
    ttb, longest = [], 0
    for col, label in TTB_ROWS:
        if g_row[col] and g_row[col] >= longest:
            ttb.append((label, g_row[col]))
            longest = g_row[col]

    catalog()                                   # make sure the cache (and weights) are loaded
    me = _cache["by_id"].get(game_id)
    rc = roms.catalog(get_db)
    # ⚙ → Recommendations, plus the user's ratings (favor) and completed games (skip)
    cfg = mine.pick_cfg(settings.current(), _cache["games"], rc["cards"], rc["reps"])
    tangential = recommend.suggestions(me, _cache["games"], _cache["weights"], _cache["n"], cfg) \
        if me and cfg["show_tangents"] else []
    retro = roms.retro_picks(me, get_db, cfg) if me and cfg["show_retro"] else []

    return render_template("game.html",
        tangential=tangential,
        retro=retro,
        my=me["my"] if me else None, my_kind="game", my_id=game_id,
        lutris_via=sorted(me["f"]["lutris"]) if me else [],
        system_labels=roms.SYSTEM_LABELS,
        game=g_row,
        ownerships=ownerships,
        compat=compat,
        playtime=playtime,
        tag_rows=tag_rows,
        developers=tags.get("developer", []),
        publishers=[p for p in tags.get("publisher", []) if p not in tags.get("developer", [])],
        series_sections=series_sections,
        ttb=ttb,
        min_ratings=MIN_RATINGS,
    )


@app.route("/api/stats")
def api_stats():
    db = get_db()
    return jsonify({
        "total_games": db.execute(
            "SELECT COUNT(DISTINCT game_id) FROM ownership WHERE kind!='launcher'"
        ).fetchone()[0],
        "igdb_matched": db.execute(
            "SELECT COUNT(*) FROM games WHERE igdb_id IS NOT NULL"
        ).fetchone()[0],
        "by_store": {
            s: db.execute(
                "SELECT COUNT(DISTINCT game_id) FROM ownership WHERE store=?", (s,)
            ).fetchone()[0]
            for s in STORE_LABELS
        },
    })


app.register_blueprint(make_roms_blueprint(get_db, games_data))
app.register_blueprint(make_scans_blueprint(get_db))
app.register_blueprint(settings.make_blueprint(games_data, lambda: roms.catalog(get_db)))
def home_facets():
    """The grid's sidebar, nothing ticked, for the home page (links open /games)."""
    g.browse_target = "/games"
    base = mine.annotate(catalog(), "game")
    return browse.build_facets(base, {}, FACETS, _label, _option_order)


app.register_blueprint(home.make_blueprint(games_data, lambda: roms.catalog(get_db), home_facets))
app.register_blueprint(mine.make_blueprint(games_data, lambda: roms.catalog(get_db)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

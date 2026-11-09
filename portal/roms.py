"""
ES-DE ROM browsing — /roms grid and /rom/<id> detail.

Data comes from the esde_entries table (filled by importers/esde.py from
ES-DE's gamelist.xml files, ~8,000 ROMs with scraped metadata) and, for the
92% matched to IGDB by match_roms.py, rom_tags: the same theme / game mode /
tag / perspective / series labels the store games have. Cover art and
screenshots are served straight from ES-DE's downloaded_media folder:
    ~/ES-DE/downloaded_media/<system>/<covers|miximages|screenshots|…>/<rom path minus extension>.<png|jpg>
(the ROM's subfolder, if any, is mirrored there).

One card per game
-----------------
The grid shows one card per game and system, not one per file: ROMs with the
same IGDB id (or, unmatched, the same scraped name) on one system are grouped
— Variants/ region copies, and main-list files ScreenScraper named alike
("90 Minutes - European Prime Goal" and "J.League Soccer Prime Goal 3").
The card is the main-list copy (_representative) with a "+N versions" badge;
every file keeps its own /rom/<id> page, which lists the others. Entries in
Translations and Hacks/ are different content, so each stays a card of its
own, labelled. Cards made only of Variants copies (no main-list copy) are
hidden unless ?variants=1.

Filtering, counts, sorting and URLs are the shared mechanics in browse.py.
"""

import pathlib, re, time
from flask import Blueprint, abort, render_template, request, send_file

import browse
import recommend
import settings
import mine

MEDIA_ROOT = pathlib.Path.home() / "ES-DE/downloaded_media"
CACHE_SECS = 600   # ROM list only changes on refresh; media scan is ~70k stat() calls

# ES-DE system folder → display name (unknown systems show the folder name)
SYSTEM_LABELS = {
    "snesna":       "SNES",
    "sfc":          "Super Famicom",
    "nes":          "NES",
    "famicom":      "Famicom",
    "n64":          "Nintendo 64",
    "gc":           "GameCube",
    "gb":           "Game Boy",
    "gbc":          "Game Boy Color",
    "gba":          "Game Boy Advance",
    "nds":          "Nintendo DS",
    "genesis":      "Genesis",
    "mastersystem": "Master System",
    "gamegear":     "Game Gear",
    "segacd":       "Sega CD",
    "psx":          "PlayStation",
    "ps2":          "PlayStation 2",
    "psp":          "PSP",
    "atari2600":    "Atari 2600",
    "apple2":       "Apple II",
    "mame":         "Arcade (MAME)",
    "pico8":        "PICO-8",
}

# ES-DE genres are hierarchical ("Sports / Baseball", "Shoot'em Up / Vertical").
# The filter uses the part before " / ", tidied through this map; anything
# not listed is used as-is.
GENRE_MAP = {
    "Role Playing Game":   "RPG",
    "Action RPG":          "RPG",
    "Tactical RPG":        "RPG",
    "Japanese RPG":        "RPG",
    "Dungeon Crawler RPG": "RPG",
    "Party-Based RPG":     "RPG",
    "Racing, Driving":     "Racing",
    "Racing TPV":          "Racing",
    "Motorcycle race TPV": "Racing",
    "Motorcycle race FPV": "Racing",
    "Horse racing":        "Racing",
    "Shoot'em Up":         "Shoot 'em up",
    "Beat'em Up":          "Beat 'em up",
    "Lightgun Shooter":    "Shooter",
    "Board game":          "Board, card & casino",
    "Asiatic board game":  "Board, card & casino",
    "Mahjong":             "Board, card & casino",
    "Shougi":              "Board, card & casino",
    "Go":                  "Board, card & casino",
    "Othello":             "Board, card & casino",
    "Renju":               "Board, card & casino",
    "Hanafuda":            "Board, card & casino",
    "Playing cards":       "Board, card & casino",
    "Casino":              "Board, card & casino",
    "Fishing":             "Hunting & fishing",
    "Hunting":             "Hunting & fishing",
    "Hunting and Fishing": "Hunting & fishing",
    "Rhythm":              "Music / rhythm",
    "Music and Dancing":   "Music / rhythm",
    "Dancing":             "Music / rhythm",
    "Quiz":                "Quiz / trivia",
    "Casual Game":         "Casual",
    "Build And Management": "Management",
    "Sports with animals": "Sports",
    "Thinking":            "Puzzle",
    "Various":             "Other",
    "Demo":                "Other",
    "Adults":              "Other",
}

# Detail page rows from IGDB (rom_tags), each value links to /roms?kind=value
DETAIL_TAG_ROWS = [
    ("theme",       "Theme"),
    ("mode",        "Modes"),
    ("perspective", "Perspective"),
    ("tag",         "Tags"),
    ("series",      "Series"),
]

PLAYER_BUCKETS = ["1 player", "2 players", "3–4 players", "5+ players"]

STATUS_LABELS = ["Favorite", "Played", "Never played", "Also in store library", "No IGDB match"]

# ROM subfolders (README §1: Variants/ = other region/language copies of a game
# in the main list; Translations and Hacks/ = fan translations and hacks)
VARIANTS_DIR = "Variants"
HACKS_DIR    = "Translations and Hacks"
COPIES_LABELS = ["Several versions", "Translation / hack", "Variant only"]

# Genre is ScreenScraper's (every ROM has one); theme / mode / tag /
# perspective / series come from IGDB (rom_tags), so unmatched ROMs have none.
FACETS = [
    ("system",      "System",      True),
    ("genre",       "Genre",       True),
    ("theme",       "Theme",       True),
    ("mode",        "Game mode",   True),
    ("tag",         "Tags",        True),
    ("players",     "Max players", False),
    ("perspective", "Perspective", False),
    ("decade",      "Decade",      False),
    ("status",      "Status",      False),
    ("copies",      "Copies",      False),
    ("mystatus",    "My status",   False),
    ("myrating",    "My rating",   False),
]
LINK_FILTERS = [("series", "Series"), ("developer", "Developer"), ("publisher", "Publisher")]
# rom_tags kinds loaded as facets
TAG_FACETS   = ("theme", "mode", "tag", "perspective", "series")
FACET_KEYS   = [f[0] for f in FACETS]
FILTER_KEYS  = FACET_KEYS + [k for k, _ in LINK_FILTERS]
HEADINGS     = {k: h for k, h, _ in FACETS} | dict(LINK_FILTERS)

SORTS = {
    "title":  "A–Z",
    "newest": "Newest",
    "oldest": "Oldest",
    "rating": "Top rated",
    "recent": "Recently played",
    "played": "Most played",
    "myrating": "My rating",
}
SORT_GETTERS = {
    "newest": (lambda r: r["year"], True),
    "oldest": (lambda r: r["year"], False),
    "rating": (lambda r: r["rating"], True),
    "recent": (lambda r: r["lastplayed"] or None, True),
    "played": (lambda r: r["playcount"] or None, True),
    "myrating": (lambda r: (r.get("my") or {}).get("rating"), True),
}

# Media folders tried, in order, for each image kind
MEDIA_KINDS = {
    "cover":      ("covers", "miximages", "titlescreens", "screenshots"),
    "screenshot": ("screenshots", "titlescreens"),
}


def display_name(es_name):
    """Unscraped entries keep the file name ("Ultima_I") — make those readable."""
    return es_name if " " in es_name else es_name.replace("_", " ")


def top_genre(genre):
    if not genre:
        return None
    top = genre.split(" / ")[0].strip()
    return GENRE_MAP.get(top, top)


def player_bucket(players):
    """ES-DE players: "1", "1-2", "1-4", "4+", … → bucket by the maximum."""
    nums = [int(n) for n in re.findall(r"\d+", players or "")]
    if not nums:
        return None
    most = max(nums)
    if most <= 1:
        return "1 player"
    if most == 2:
        return "2 players"
    return "3–4 players" if most <= 4 else "5+ players"


def release_year(date):
    """ES-DE dates look like 19910823T000000."""
    year = int(date[:4]) if date and date[:4].isdigit() else None
    return year if year and year >= 1970 else None


def find_media(system, rom_path, kind):
    """Path relative to MEDIA_ROOT of the first existing image, or None."""
    rel = pathlib.PurePosixPath(rom_path.removeprefix("./"))
    base = rel.parent / rel.stem          # keep the ROM's subfolder, drop the extension
    for folder in MEDIA_KINDS[kind]:
        for ext in (".png", ".jpg"):
            candidate = pathlib.Path(system, folder, f"{base}{ext}")
            if (MEDIA_ROOT / candidate).is_file():
                return candidate
    return None


# igdb_relations.relation → label, as seen from each page.
# Rows read: "related_igdb_id is a <relation> of igdb_id" (igdb_id = the ROM's game).
RETRO_LABELS  = {"remake": "The original", "remaster": "The original",   # game page: the ROM is…
                 "port": "Another version", "original": "An edition of it"}
MODERN_LABELS = {"remake": "Remake", "remaster": "Remaster",              # ROM page: the game is…
                 "port": "Another version", "original": "The original"}

_cache = {"at": 0.0, "roms": None}


def subfolder(rom_path):
    """First folder of a gamelist path ("Variants" for ./Variants/x.zip), "" in the main list."""
    parts = rom_path.removeprefix("./").split("/")
    return parts[0] if len(parts) > 1 else ""


def _representative(rom):
    """Sort key: which of several ROMs of one game stands for them (cards, picks)."""
    in_subfolder = subfolder(rom["rom_path"]) in (VARIANTS_DIR, HACKS_DIR)
    return (in_subfolder, rom["cover"] is None, -rom["playcount"], len(rom["title"]))


def _group_key(rom):
    """Grid card a ROM belongs to: one per game and system; hacks always their own."""
    if subfolder(rom["rom_path"]) == HACKS_DIR:
        return ("rom", rom["id"])
    if rom["igdb_id"]:
        return (rom["system"], "igdb", rom["igdb_id"])
    return (rom["system"], "name", re.sub(r"[^a-z0-9]", "", rom["title_lower"]))


def _cards(roms):
    """(cards, {rom id: card}): the grid items, one per _group_key."""
    groups = {}
    for rom in roms:
        groups.setdefault(_group_key(rom), []).append(rom)
    cards, card_of = [], {}
    for members in groups.values():
        members.sort(key=_representative)
        rep = members[0]
        folders = {subfolder(m["rom_path"]) for m in members}
        copies = set()
        if len(members) > 1:
            copies.add("Several versions")
        if folders == {HACKS_DIR}:
            copies.add("Translation / hack")
        if folders == {VARIANTS_DIR}:
            copies.add("Variant only")
        status = set().union(*(m["f"]["status"] for m in members))
        if "Played" in status:
            status.discard("Never played")
        card = {**rep,
                "versions":   members,
                "hack":       folders == {HACKS_DIR},
                "favorite":   any(m["favorite"] for m in members),
                "playcount":  sum(m["playcount"] for m in members),
                "lastplayed": max((m["lastplayed"] for m in members if m["lastplayed"]), default=None),
                "playtime_secs": sum(m["playtime_secs"] for m in members),
                "f": {**rep["f"], "status": status, "copies": copies}}
        cards.append(card)
        card_of.update((m["id"], card) for m in members)
    return cards, card_of


def _load(db):
    tags = {}
    for r in db.execute("SELECT rom_id, kind, value FROM rom_tags"):
        tags.setdefault(r["rom_id"], {}).setdefault(r["kind"], set()).add(r["value"])
    roms = []
    for r in db.execute("""
        SELECT id, game_id_fk, igdb_id, system, rom_path, es_name, genre, developer, publisher,
               players, rating, release_date, favorite, playcount, lastplayed, playtime_secs
        FROM esde_entries
    """):
        name   = display_name(r["es_name"])
        year   = release_year(r["release_date"])
        status = set()
        if r["favorite"]:
            status.add("Favorite")
        status.add("Played" if r["playcount"] else "Never played")
        if r["game_id_fk"]:
            status.add("Also in store library")
        if not r["igdb_id"]:
            status.add("No IGDB match")
        mine = tags.get(r["id"], {})
        traits = recommend.trait_set((k, v) for k in recommend.TRAIT_KINDS for v in mine.get(k, ()))
        roms.append({
            "id":          r["id"],
            "kind":        "rom",                     # vs "game" (home page rows)
            "igdb_id":     r["igdb_id"],
            "rom_path":    r["rom_path"],
            "title":       name,
            "title_lower": name.lower(),
            "search":      browse.search_text(name),
            "system":      r["system"],
            "year":        year,
            "rating":      round(r["rating"] * 100) if r["rating"] else None,
            "players":     r["players"],
            "favorite":    bool(r["favorite"]),
            "playcount":   r["playcount"] or 0,
            "lastplayed":  r["lastplayed"],
            "playtime_secs": r["playtime_secs"] or 0,
            "cover":       find_media(r["system"], r["rom_path"], "cover"),
            "f": {
                "system":    {r["system"]},
                "genre":     {top_genre(r["genre"])} - {None},
                "players":   {player_bucket(r["players"])} - {None},
                "decade":    {f"{year // 10 * 10}s"} if year else set(),
                "status":    status,
                "developer": {r["developer"]} if r["developer"] else set(),
                "publisher": {r["publisher"]} if r["publisher"] else set(),
                **{k: mine.get(k, set()) for k in TAG_FACETS},
            },
            "traits": traits,
            # labels for exclusions (⚙ → Recommendations): IGDB genres too
            "labels": recommend.item_labels(
                {**mine, "genre": {top_genre(r["genre"])} - {None}}, mine.get("keyword", ()),
                extra={("genre", g) for g in mine.get("genre", ())}),
            # IGDB labels for Retro/Modern Picks (genre here is IGDB's, normalized
            # like the store games', not ScreenScraper's)
            "rel": recommend.rel_sets({k: mine.get(k, set()) for k in ("series", "tag", "theme", "genre", "perspective")},
                                      traits),
        })

    # One ROM per IGDB game for the pick rows (no "SNES + Japanese variant + GB
    # port" triplets): the main-folder copy with a cover that has been played most
    groups = {}
    for rom in roms:
        if rom["igdb_id"]:
            groups.setdefault(rom["igdb_id"], []).append(rom)
    rep_by_igdb = {i: min(g, key=_representative) for i, g in groups.items()}
    reps = list(rep_by_igdb.values())
    weights, n = recommend.build_weights(reps)

    by_rom_igdb, by_game_igdb = {}, {}
    for r in db.execute("SELECT igdb_id, related_igdb_id, relation FROM igdb_relations"):
        by_rom_igdb.setdefault(r["igdb_id"], []).append((r["related_igdb_id"], r["relation"]))
        by_game_igdb.setdefault(r["related_igdb_id"], []).append((r["igdb_id"], r["relation"]))

    cards, card_of = _cards(roms)
    # Tangents pool: one ROM per IGDB game, plus the unmatched games' cards
    # (no traits, so they only take part in the generic fill tiers)
    pick_pool = reps + [c for c in cards
                        if not c["igdb_id"] and "Variant only" not in c["f"]["copies"]]
    return {
        "roms": roms, "by_id": {r["id"]: r for r in roms},
        "cards": cards, "card_of": card_of, "pick_pool": pick_pool,
        "reps": reps, "rep_by_igdb": rep_by_igdb,
        "weights": weights, "n": n,
        "rel_weights": recommend.build_rel_weights(reps),
        "relations_by_rom_igdb": by_rom_igdb, "relations_by_game_igdb": by_game_igdb,
    }


def catalog(get_db):
    """ROM grid items plus the pick-row data, cached CACHE_SECS."""
    if _cache["roms"] is None or time.time() - _cache["at"] > CACHE_SECS:
        _cache.update(_load(get_db()))
        _cache["at"] = time.time()
    return _cache


def retro_picks(game, get_db, cfg):
    """Retro Picks for a store game (an app.py catalog item): related ROMs. cfg: settings.current()."""
    c = catalog(get_db)
    labels = {}
    if game["igdb_id"]:
        same = c["rep_by_igdb"].get(game["igdb_id"])
        if same:
            labels[same["id"]] = "Same game"
        for rom_igdb, relation in c["relations_by_game_igdb"].get(game["igdb_id"], ()):
            rep = c["rep_by_igdb"].get(rom_igdb)
            if rep:
                labels.setdefault(rep["id"], RETRO_LABELS[relation])
    return recommend.related(game, c["reps"], c["rel_weights"], labels,
                             limit=cfg["retro_picks"], exclude=cfg["exclude_set"],
                             skip_completed=cfg["skip_completed"], liked=cfg["liked"])


def make_blueprint(get_db, games_data):
    """games_data() → app.py's store-game cache (games, by_igdb, rel_weights)."""
    bp = Blueprint("roms", __name__)

    def label(facet, value):
        return SYSTEM_LABELS.get(value, value) if facet == "system" else value

    def order(facet, counts):
        if facet == "players":
            return [b for b in PLAYER_BUCKETS if b in counts]
        if facet == "status":
            return [s for s in STATUS_LABELS if s in counts]
        if facet == "mystatus":
            return [s for s in mine.MYSTATUS_LABELS if s in counts]
        if facet == "myrating":
            return [s for s in mine.MYRATING_LABELS if s in counts]
        if facet == "copies":
            return [s for s in COPIES_LABELS if s in counts]
        if facet == "decade":
            return sorted(counts, reverse=True)
        return browse.by_count(counts)

    @bp.route("/roms")
    def rom_index():
        q    = request.args.get("q", "").strip()
        sort = request.args.get("sort", "title")
        if sort not in SORTS:
            sort = "title"

        selected = browse.selected_filters(FILTER_KEYS)
        base = mine.annotate(catalog(get_db)["cards"], "rom")   # the user's status/rating
        if q:
            base = browse.search(base, q)
        show_variants = request.args.get("variants") == "1"
        hidden_variants = 0
        if not show_variants:
            hidden_variants = sum(1 for c in base if "Variant only" in c["f"]["copies"])
            base = [c for c in base if "Variant only" not in c["f"]["copies"]]
        results = browse.sort_items([r for r in base if browse.matches(r, selected)],
                                    sort, SORT_GETTERS)
        roms, page, total_pages = browse.paginate(results)

        return render_template("roms.html",
            roms=roms, total=len(results), page=page, total_pages=total_pages,
            q=q, sort=sort, sorts=SORTS, system_labels=SYSTEM_LABELS,
            facets=browse.build_facets(base, selected, FACETS, label, order),
            chips=browse.chips(selected, FILTER_KEYS, HEADINGS, label),
            n_active=sum(len(v) for v in selected.values()),
            clear_url=browse.clear_url(),
            show_variants=show_variants, hidden_variants=hidden_variants,
            variants_url=browse.toggle_url("variants", "1"),
        )

    @bp.route("/rom/<int:rom_id>")
    def rom_detail(rom_id):
        db = get_db()
        row = db.execute("SELECT * FROM esde_entries WHERE id = ?", (rom_id,)).fetchone()
        if not row:
            abort(404)
        linked = None
        if row["game_id_fk"]:
            linked = db.execute("SELECT id, canonical_title FROM games WHERE id = ?",
                                (row["game_id_fk"],)).fetchone()
        year = release_year(row["release_date"])
        tags = {}
        for r in db.execute("SELECT kind, value FROM rom_tags WHERE rom_id = ? "
                            "ORDER BY value COLLATE NOCASE", (rom_id,)):
            tags.setdefault(r["kind"], []).append(r["value"])
        c = catalog(get_db)
        rom = c["by_id"].get(rom_id)
        card = c["card_of"].get(rom_id)
        versions = [{"id": m["id"], "path": m["rom_path"].removeprefix("./"),
                     "folder": subfolder(m["rom_path"]), "current": m["id"] == rom_id}
                    for m in (card["versions"] if card else [])]
        games = games_data()
        # ⚙ → Recommendations, plus the user's ratings (favor) and completed games (skip)
        cfg = mine.pick_cfg(settings.current(), games["games"], c["cards"], c["pick_pool"])
        tangential, modern = [], []
        if rom and cfg["show_tangents"]:
            # its IGDB game's stand-in, or for unmatched ROMs its card
            me = c["rep_by_igdb"][rom["igdb_id"]] if rom["igdb_id"] else card
            tangential = recommend.suggestions(me, c["pick_pool"], c["weights"], c["n"], cfg)
        if rom and rom["igdb_id"] and cfg["show_modern"]:
            labels = {}
            same = games["by_igdb"].get(rom["igdb_id"])
            if same:
                labels[same["id"]] = "Same game"
            for game_igdb, relation in c["relations_by_rom_igdb"].get(rom["igdb_id"], ()):
                if game_igdb in games["by_igdb"]:
                    labels.setdefault(games["by_igdb"][game_igdb]["id"], MODERN_LABELS[relation])
            modern = recommend.related(rom, games["games"], games["rel_weights"], labels,
                                       limit=cfg["modern_picks"], exclude=cfg["exclude_set"],
                                       skip_completed=cfg["skip_completed"], liked=cfg["liked"])
        return render_template("rom.html",
            tangential=tangential, modern=modern, system_labels=SYSTEM_LABELS,
            my=card["my"] if card else None, my_kind="rom", my_id=rom_id,
            versions=versions if len(versions) > 1 else [],
            folder=subfolder(row["rom_path"]), hacks_dir=HACKS_DIR, variants_dir=VARIANTS_DIR,
            tag_rows=[(k, h, tags[k]) for k, h in DETAIL_TAG_ROWS if tags.get(k)],
            rom=row, title=display_name(row["es_name"]), year=year,
            genre_top=top_genre(row["genre"]), players_bucket=player_bucket(row["players"]),
            rating=round(row["rating"] * 100) if row["rating"] else None,
            system_label=SYSTEM_LABELS.get(row["system"], row["system"]),
            has_cover=find_media(row["system"], row["rom_path"], "cover") is not None,
            has_screenshot=find_media(row["system"], row["rom_path"], "screenshot") is not None,
            linked=linked,
        )

    @bp.route("/rom-media/<int:rom_id>/<kind>")
    def rom_media(rom_id, kind):
        if kind not in MEDIA_KINDS:
            abort(404)
        row = get_db().execute("SELECT system, rom_path FROM esde_entries WHERE id = ?",
                               (rom_id,)).fetchone()
        rel = row and find_media(row["system"], row["rom_path"], kind)
        if not rel:
            abort(404)
        path = (MEDIA_ROOT / rel).resolve()
        if not path.is_relative_to(MEDIA_ROOT.resolve()):   # rom_path comes from gamelist.xml
            abort(404)
        return send_file(path, max_age=86400)

    return bp

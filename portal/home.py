"""
Home page — "/" (the store-game grid moved to /games).

Three parts, top to bottom:

  Play next             automatic, no buttons to set: games marked In
                        progress (best-rated first), then games like the ones
                        rated highly ("Like StarCraft"). The same on every
                        visit. Only the user's marks count, not Steam or ES-DE
                        play history (see mine.py for why).
  Where are you playing?  one button per device (DEVICES). The chosen one
                        shows a row of picks that play there.
  Scratch an itch…      one button per kind of game (ITCHES): retro,
                        relaxing, strategy, … The chosen one shows a row of
                        picks. Or a search box for any tag the games carry
                        (/?find=talking+animals): FIND_HITS games with that
                        tag plus FIND_TANGENTS "tangents" (see _find below).

The buttons are plain links: /?for=odin&itch=scifi. (The search is
"find", not "tag": /?tag=Relaxing is an old grid link.) A device and an itch
can be open at the same time; clicking an open button closes it. Every
load draws a new set of picks (the Shuffle link just reloads), weighted
toward well-rated games and toward themes of games rated highly.

Picks leave out what every pick row leaves out (⚙ → Recommendations: the
excluded labels, games marked Complete) and also games marked In progress,
which are already under Play next.

The left sidebar is the /games grid's filter list, so filtering is one
click from here; its links open /games.

Any other query string on "/" is an old grid link (/?genre=RPG) and is
redirected to /games with the same parameters.

To change a device or an itch, edit DEVICES / ITCHES below: each has a
test function that gets one item and returns a short reason shown under
its cover (truthy = it's a match), or None.
"""

import math, random
from urllib.parse import urlencode
from flask import Blueprint, redirect, render_template, request

import mine
import recommend
import roms
import settings

ROW = 12            # picks per device / itch row
PLAY_NEXT_MAX = 12  # cards in the Play next row
LIKE_SOURCES = 3    # "Like X" picks come from this many top-rated games …
LIKE_EACH = 3       # … this many per game
DRAW_POOL = 60      # picks are drawn from this many best-scoring matches
FIND_HITS = 8       # tag search: games with the tag …
FIND_TANGENTS = 4   # … plus games without it that share what those games have in common
FIND_MIN_GAMES = 2  # a keyword needs this many store games to be offered in the search list
FIND_FEW = 3        # a tag on this many games or fewer has no pattern to learn: see _find
UNKNOWN_RATING = 62 # score for a game with no (or too few) critic ratings
MIN_RATINGS = 5     # same as app.py: fewer reviews than this = no rating


# ── what counts as what ───────────────────────────────────────────────────────
# Tests get an item (store game or ROM) and read item["labels"]: a set of
# (kind, value) pairs, kind one of genre/theme/tag/perspective/mode
# (recommend.item_labels; for ROMs the genres are IGDB's).

def _first(item, wanted):
    """The first (kind, value) in wanted that the item has → its value, else None."""
    for pair in wanted:
        if pair in item["labels"]:
            return pair[1]
    return None


KBM = [("genre", "Real-time strategy"), ("theme", "4X"), ("tag", "City builder"),
       ("genre", "Point-and-click"), ("tag", "Management"), ("theme", "Business"),
       ("genre", "MOBA"), ("perspective", "Text")]

# (City builder isn't here: on IGDB it covers Civilization, which is anything but)
RELAXING = [("tag", "Relaxing"), ("tag", "Walking simulator"),
            ("genre", "Puzzle"), ("genre", "Card & board")]
NOT_RELAXING = {("theme", "Horror"), ("theme", "Thriller"), ("theme", "Warfare"),
                ("theme", "Survival"), ("tag", "Difficult"), ("tag", "Permadeath"),
                ("tag", "Survival horror"), ("tag", "Zombies")}

STRATEGY = [("genre", "Real-time strategy"), ("genre", "Turn-based strategy"),
            ("theme", "4X"), ("genre", "Tactics"), ("genre", "Strategy")]

SCIFI = [("tag", "Space"), ("tag", "Cyberpunk"), ("theme", "Science fiction")]

COOP = [("mode", "Split screen"), ("mode", "Co-operative")]


def _relaxing(it):
    return None if it["labels"] & NOT_RELAXING else _first(it, RELAXING)


def _coop(it):
    hit = _first(it, COOP)
    return {"Split screen": "Split screen", "Co-operative": "Co-op"}.get(hit)


def _short(it):
    if it.get("ttb") and it["ttb"] < 5:
        return f"About {max(1, round(it['ttb']))} h"
    return "Roguelike" if ("tag", "Roguelike") in it["labels"] else None


def _retro(it):
    if it["kind"] == "rom":
        return roms.SYSTEM_LABELS.get(it["system"], it["system"])
    return f"PC, {it['year']}" if it["year"] and it["year"] < 2000 else None


# Store labels for the device reason line
STORE_SHORT = {"steam": "Steam", "epic": "Epic", "gog": "GOG", "amazon": "Prime",
               "battlenet": "Battle.net", "ea": "EA App", "ubisoft": "Ubisoft", "lutris": "Lutris"}


def _ebonhawk(it):
    """Ebon Hawk: ROMs (ES-DE) and every PC store library but Game Pass (no Xbox app on Linux)."""
    if it["kind"] == "rom":
        return roms.SYSTEM_LABELS.get(it["system"], it["system"])
    stores = [STORE_SHORT[s] for s in it["stores"] if s in STORE_SHORT]
    if stores:
        return stores[0]
    if it["f"]["lutris"]:
        return f"Lutris: {sorted(it['f']['lutris'])[0]}"
    return None


def _odin(it):
    """
    Odin 2: ROMs, Game Pass games DroidDeck installs, Game Pass cloud
    streaming. Keyboard-and-mouse games are left out (it's a handheld).
    """
    if it["kind"] == "rom":
        return roms.SYSTEM_LABELS.get(it["system"], it["system"])
    if _first(it, KBM):
        return None
    if it["f"]["droid"] & {"Installable", "Likely installable"}:
        return "DroidDeck"
    if "Cloud streamable" in it["f"]["gp"]:
        return "Xbox Cloud"
    return None


def _xbox(it):
    return "Game Pass" if "Xbox console" in it["f"]["gp"] else None


def _kbm(it):
    # PC only: Game Pass PC titles count (they run on a Windows PC or in the cloud)
    return _first(it, KBM)


# key → dict; order = button order.
#   test:  item → reason or None
#   roms:  share of the row given to ROMs (0 = store games only)
#   browse: a /games or /roms link with roughly the same games, or None
DEVICES = {
    "ebonhawk": dict(name="Ebon Hawk", icon="📺", sub="TV PC",
                     blurb="Steam, Epic, GOG, Prime and the Lutris launchers, plus the ES-DE ROMs.",
                     test=_ebonhawk, roms=0.25, browse=None),
    "odin":     dict(name="Odin 2", icon="🕹️", sub="Handheld",
                     blurb="ROMs, Game Pass games DroidDeck can install, and Xbox Cloud streaming. "
                           "No keyboard-and-mouse games.",
                     test=_odin, roms=1 / 3, browse="/games?droid=Installable&droid=Likely+installable"),
    "xbox":     dict(name="Xbox", icon="🎮", sub="Console",
                     blurb="Game Pass games on the console list.",
                     test=_xbox, roms=0, browse="/games?gp=Xbox+console"),
}

ITCHES = {
    "retro":    dict(name="Retro", icon="👾", blurb="ROMs, and PC games from before 2000.",
                     test=_retro, roms=2 / 3, browse="/roms"),
    "relaxing": dict(name="Relaxing", icon="🌿", blurb="Relaxing, puzzle and card games. Nothing scary or punishing.",
                     test=_relaxing, roms=0.25, browse="/games?tag=Relaxing"),
    "strategy": dict(name="Strategy", icon="♟️", blurb="Real-time, turn-based, tactics and 4X.",
                     test=lambda it: _first(it, STRATEGY), roms=0,
                     browse="/games?genre=Strategy&genre=Real-time+strategy&genre=Turn-based+strategy&genre=Tactics"),
    "scifi":    dict(name="Sci-fi", icon="🚀", blurb="Science fiction, space and cyberpunk.",
                     test=lambda it: _first(it, SCIFI), roms=0, browse="/games?theme=Science+fiction"),
    "kbm":      dict(name="Keyboard & mouse", icon="⌨️",
                     blurb="Games that play best at a desk: RTS, 4X, city builders, management, point-and-click.",
                     test=_kbm, roms=0,
                     browse="/games?genre=Real-time+strategy&genre=Point-and-click&tag=City+builder&tag=Management"),
    "coop":     dict(name="Co-op", icon="🤝", blurb="Co-op and split-screen games, to play together.",
                     test=_coop, roms=0.25, browse="/games?mode=Co-operative&mode=Split+screen"),
    "short":    dict(name="Short sessions", icon="⏱️",
                     blurb="Under 5 hours to finish, or roguelikes you can drop in and out of.",
                     test=_short, roms=0, browse="/games?length=Under+5+hours"),
}


# ── scoring and drawing ───────────────────────────────────────────────────────

def _quality(it):
    if it["kind"] == "rom":
        q = it.get("rating") or UNKNOWN_RATING          # ScreenScraper, 0–100
        return q * (1.3 if it.get("favorite") else 1)
    return it["rating"] if it.get("rating") and it.get("rating_count", 0) >= MIN_RATINGS \
        else UNKNOWN_RATING


def _score(it, liked):
    s = _quality(it) / 100 * recommend._liked_boost(it, liked)
    return s if it.get("cover") else s * 0.4            # coverless cards look broken in a row


def _eligible(it, cfg):
    status = (it.get("my") or {}).get("status")
    if status == "in_progress" or (status == "complete" and cfg["skip_completed"]):
        return False
    return not (cfg["exclude_set"] and it["labels"] & cfg["exclude_set"])


def _draw(scored, n, rng):
    """n items from the DRAW_POOL best (score, item, reason), weighted toward the top."""
    pool = sorted(scored, key=lambda x: -x[0])[:DRAW_POOL]
    out = []
    while pool and len(out) < n:
        i = rng.choices(range(len(pool)), weights=[x[0] ** 4 for x in pool])[0]
        out.append(pool.pop(i))
    return out


def _row(spec, games, rom_pool, cfg, rng):
    """[{kind, game, why}] for one device or itch."""
    def matches(items):
        out = []
        for it in items:
            if _eligible(it, cfg):
                why = spec["test"](it)
                if why:
                    out.append((_score(it, cfg["liked"]), it, why))
        return out

    n_roms = round(ROW * spec["roms"]) if spec["roms"] else 0
    picked_roms = _draw(matches(rom_pool), n_roms, rng) if n_roms else []
    picked = _draw(matches(games), ROW - len(picked_roms), rng)
    # store games first, then ROMs: keeps a row from looking like a random jumble
    return [{"kind": it["kind"], "game": it, "why": why} for _, it, why in picked + picked_roms]


def _play_next(games, cards, rom_pool, games_cache, roms_cache, cfg):
    """[{kind, game, why}]: In progress, then "Like X"."""
    items = [g for g in games] + [c for c in cards]
    out, seen = [], set()

    def add(it, why):
        key = (it["kind"], it["id"])
        if key not in seen and len(out) < PLAY_NEXT_MAX:
            seen.add(key)
            out.append({"kind": it["kind"], "game": it, "why": why})

    in_progress = [it for it in items if (it["my"] or {}).get("status") == "in_progress"]
    for it in sorted(in_progress, key=lambda it: (-((it["my"] or {}).get("rating") or 0),
                                                  it["title_lower"])):
        add(it, "In progress")

    # "Like X": related() picks for the user's best-rated games (store games
    # from store games, ROMs from ROMs: those are the pools related() knows)
    rated = sorted((it for it in items if (it["my"] or {}).get("rating")),
                   key=lambda it: -it["my"]["rating"])
    sources = [it for it in rated if it["my"]["rating"] >= cfg["favor_min_rating"]][:LIKE_SOURCES]
    for src in sources:
        if src["kind"] == "game":
            pool, weights = games, games_cache["rel_weights"]
        else:
            pool, weights = rom_pool, roms_cache["rel_weights"]
        picks = recommend.related(src, pool, weights, limit=LIKE_EACH + 3,
                                  exclude=cfg["exclude_set"], skip_completed=cfg["skip_completed"],
                                  liked=cfg["liked"])
        added = 0
        for p in picks:
            if added < LIKE_EACH and _eligible(p["game"], cfg) and \
                    (p["game"]["kind"], p["game"]["id"]) not in seen:
                add(p["game"], f"Like {src['title']}")
                added += 1
    return out


# ── tag search ────────────────────────────────────────────────────────────────
# "Tags" here = every label a store game or ROM carries: genre, theme, curated
# tag, mode, perspective, and IGDB keywords minus recommend.NOISE. Matching is
# case-insensitive across kinds, so "Roguelike" finds both the curated tag and
# the keyword.

FACET_KINDS = ("tag", "theme", "genre", "perspective", "mode")   # /games has a filter for these
_vocab_cache = {"key": None}


def _tag_values(it):
    """Lower-cased tag values of one item (store game or ROM)."""
    return {v.lower() for k, v in it["labels"]
            if k != "keyword" or not recommend.NOISE.search(v)}


def _vocabulary(games):
    """{lower value: {"name", "kinds", "n"}} over store games; cached per catalog load."""
    if _vocab_cache["key"] is not id(games):
        vocab = {}
        for g in games:
            for k, v in g["labels"]:
                if k == "keyword" and recommend.NOISE.search(v):
                    continue
                e = vocab.setdefault(v.lower(), {"name": v, "kinds": set(), "n": 0})
                e["kinds"].add(k)
                if k != "keyword":
                    e["name"] = v                       # prefer the facet's capitalization
        for g in games:
            for v in _tag_values(g):
                vocab[v]["n"] += 1
        vocab = {v: e for v, e in vocab.items()
                 if e["kinds"] - {"keyword"} or e["n"] >= FIND_MIN_GAMES}
        for e in vocab.values():
            e["name"] = e["name"][:1].upper() + e["name"][1:]
            kind = next((k for k in FACET_KINDS if k in e["kinds"]), None)
            e["browse"] = f"/games?{urlencode({kind: e['name']})}" if kind else None
        _vocab_cache.update(key=id(games), vocab=vocab,
                            names=sorted((e["name"] for e in vocab.values()), key=str.lower))
    return _vocab_cache


# IGDB keywords on lots of unrelated games that say nothing about what a game
# is like ("Time travel" games share "human" and "male protagonist"; that's not
# a reason). Tag tangents only: Tangents on game pages weigh these down by
# rarity already.
BLAND = {"human", "male protagonist", "female protagonist", "voice acting", "death", "explosion",
         "bloody", "melee", "non-player character", "boss fight", "action-adventure", "swimming",
         "unlockables", "optional objective", "tooltips", "real-time combat", "multiple endings",
         "sequel", "silent protagonist", "nameless protagonist", "character creation", "ambient music"}


def _features(it):
    """What a game "is like", for tag tangents: its theme/tag/genre/perspective labels and clean keywords."""
    return {(k, v) for k, v in it["labels"] if k in ("theme", "tag", "genre", "perspective")} \
        | {t for t in it["traits"] if t[0] == "keyword" and t[1] not in BLAND}


def _find(value, games, rom_pool, cfg, rng, gc):
    """
    (hits, tangents) for one tag value (lower case).

    hits: FIND_HITS games with the tag, drawn like the itch rows (store games
    first; ROMs fill in when there aren't enough).

    tangents: FIND_TANGENTS store games WITHOUT the tag that share what the
    tagged games have in common. Every feature (_features) of the tagged
    games is weighted by how typical it is of them (share of tagged games
    that have it) × how rare it is in the whole library (idf), keeping only
    features at least twice as common among them as overall — so "talking
    animals" leads to its comedy and kids games, not to "Action". The top
    20 features then score every untagged game; it needs two of them.
    A tag on FIND_FEW games or fewer has no pattern (with 2 games, every odd
    keyword either has "counts"), so those get the game pages' Tangents
    (recommend.suggestions) of the tagged games instead, taken in turn.
    """
    tagged = [g for g in games if value in _tag_values(g)]
    eligible_tagged = [g for g in tagged if _eligible(g, cfg)]
    hits = _draw([(_score(g, cfg["liked"]), g, _year_rating(g)) for g in eligible_tagged], FIND_HITS, rng)
    if len(hits) < FIND_HITS:
        roms_tagged = [r for r in rom_pool if _eligible(r, cfg) and value in _tag_values(r)]
        hits += _draw([(_score(r, cfg["liked"]), r, roms.SYSTEM_LABELS.get(r["system"], r["system"]))
                       for r in roms_tagged], FIND_HITS - len(hits), rng)
    hits = [{"kind": it["kind"], "game": it, "why": why} for _, it, why in hits]

    tangents = []
    if 0 < len(tagged) <= FIND_FEW:
        tagged_ids = {g["id"] for g in tagged}
        rows = [recommend.suggestions(g, games, gc["weights"], gc["n"], cfg, rng) for g in tagged]
        seen = set(tagged_ids)
        for i in range(max(map(len, rows))):
            for row in rows:
                if i < len(row) and len(tangents) < FIND_TANGENTS:
                    p = row[i]
                    because = _reasons(b for b in p["because"] if b.lower() not in BLAND)
                    if because and p["game"]["id"] not in seen and _eligible(p["game"], cfg):
                        seen.add(p["game"]["id"])
                        tangents.append({"kind": "game", "game": p["game"], "why": because})
    elif tagged:
        n_all, m = len(games), len(tagged)
        df, dt_ = {}, {}
        for g in games:
            for f in _features(g):
                df[f] = df.get(f, 0) + 1
        for g in tagged:
            for f in _features(g):
                dt_[f] = dt_.get(f, 0) + 1
        min_c = 1 if m < 4 else max(2, math.ceil(0.1 * m))
        weights = {f: (c / m) * math.log(n_all / df[f]) for f, c in dt_.items()
                   if c >= min_c and f[1].lower() != value and (c / m) >= 2 * df[f] / n_all}
        top = dict(sorted(weights.items(), key=lambda x: -x[1])[:20])
        tagged_ids = {g["id"] for g in tagged}
        scored = []
        for g in games:
            if g["id"] in tagged_ids or not _eligible(g, cfg):
                continue
            shared = sorted((f for f in _features(g) if f in top), key=lambda f: -top[f])
            if len(shared) >= 2:
                score = sum(top[f] for f in shared) * _score(g, cfg["liked"])
                why = _reasons(recommend._display(f) for f in shared)
                scored.append((score, g, why))
        # weight by score² like Tangents on game pages, not ^4: these should surprise a little
        pool = sorted(scored, key=lambda x: -x[0])[:30]
        while pool and len(tangents) < FIND_TANGENTS:
            i = rng.choices(range(len(pool)), weights=[x[0] ** 2 for x in pool])[0]
            _, g, why = pool.pop(i)
            tangents.append({"kind": "game", "game": g, "why": why})
    return hits, tangents, len(tagged)


def _reasons(names, n=2):
    """
    First n distinct reasons, joined. The tag "Story rich" and the keyword
    "story rich" are one, and so are "Procedural generation" / "Procedurally
    generated" (same word stem, recommend._same_word, cut to 9 letters).
    """
    out, keys = [], set()
    for name in names:
        key = recommend._same_word(name)[:9]
        if key not in keys:
            keys.add(key)
            out.append(name)
    return " · ".join(out[:n])


def _year_rating(g):
    """Reason line for a tag hit: its critic rating, if it has one."""
    if g.get("rating") and g.get("rating_count", 0) >= MIN_RATINGS:
        return f"★ {round(g['rating'])}"
    return ""


# ── route ─────────────────────────────────────────────────────────────────────

HOME_PARAMS = {"for", "itch", "find", "shuffle"}


def _toggle(param, key):
    """
    Link for a button: open `key` under `param` (or close it if open), keep the
    device choice. An itch button and the tag search replace each other.
    """
    args = {k: v for k, v in request.args.items() if k in ("for", "itch", "find")}
    if param == "itch":
        args.pop("find", None)
    if args.get(param) == key:
        del args[param]
    else:
        args[param] = key
    return f"/?{urlencode(sorted(args.items()))}#{param}" if args else f"/#{param}"


def make_blueprint(games_data, roms_data, sidebar_facets):
    """
    games_data() / roms_data(): app.py's store-game cache and roms.catalog().
    sidebar_facets(): the store-game grid's filter sidebar (its links open /games).
    """
    bp = Blueprint("home", __name__)

    @bp.route("/")
    def home():
        if set(request.args) - HOME_PARAMS:              # an old grid link: /?genre=RPG
            return redirect(f"/games?{request.query_string.decode()}", code=301)

        gc, rc = games_data(), roms_data()
        cfg = mine.pick_cfg(settings.current(), gc["games"], rc["cards"], rc["reps"])
        rng = random.Random()

        device = request.args.get("for") if request.args.get("for") in DEVICES else None
        itch = request.args.get("itch") if request.args.get("itch") in ITCHES else None

        # tag search: exact tag, else the one tag containing the text, else a list to choose from
        voc = _vocabulary(gc["games"])
        find = " ".join(request.args.get("find", "").split())
        found = hits = tangents = None
        choices, n_tagged = [], 0
        if find:
            itch = None
            q = find.lower()
            if q not in voc["vocab"]:
                near = sorted((v for v in voc["vocab"] if q in v), key=lambda v: -voc["vocab"][v]["n"])
                if len(near) == 1:
                    q = near[0]
                else:
                    choices = [voc["vocab"][v] for v in near[:15]]
            if q in voc["vocab"]:
                found = voc["vocab"][q]
                hits, tangents, n_tagged = _find(q, gc["games"], rc["reps"], cfg, rng, gc)

        return render_template(
            "home.html",
            play_next=_play_next(gc["games"], rc["cards"], rc["reps"], gc, rc, cfg),
            devices=DEVICES, itches=ITCHES, device=device, itch=itch,
            device_row=_row(DEVICES[device], gc["games"], rc["reps"], cfg, rng) if device else [],
            itch_row=_row(ITCHES[itch], gc["games"], rc["reps"], cfg, rng) if itch else [],
            toggle=_toggle, system_labels=roms.SYSTEM_LABELS, facets=sidebar_facets(),
            find=find, found=found, find_hits=hits or [], find_tangents=tangents or [],
            find_choices=choices, n_tagged=n_tagged, tag_names=voc["names"],
            find_link=lambda name: f"/?{urlencode([*([('for', device)] if device else []), ('find', name)])}#itch",
        )

    return bp

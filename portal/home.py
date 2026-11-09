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
                        picks.

The buttons are plain links: /?for=odin&itch=scifi. A device and an itch
can be open at the same time; clicking an open button closes it. Every
load draws a new set of picks (the Shuffle link just reloads), weighted
toward well-rated games and toward themes of games rated highly.

Picks leave out what every pick row leaves out (⚙ → Recommendations: the
excluded labels, games marked Complete) and also games marked In progress,
which are already under Play next.

Any other query string on "/" is an old grid link (/?genre=RPG) and is
redirected to /games with the same parameters.

To change a device or an itch, edit DEVICES / ITCHES below: each has a
test function that gets one item and returns a short reason shown under
its cover (truthy = it's a match), or None.
"""

import datetime as dt, random
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


# ── route ─────────────────────────────────────────────────────────────────────

HOME_PARAMS = {"for", "itch", "shuffle"}


def _toggle(param, key):
    """Link for a button: open `key` under `param` (or close it if open), keep the other choice."""
    args = {k: v for k, v in request.args.items() if k in ("for", "itch")}
    if args.get(param) == key:
        del args[param]
    else:
        args[param] = key
    query = "&".join(f"{k}={v}" for k, v in sorted(args.items()))
    return f"/?{query}#{param}" if query else f"/#{param}"


def make_blueprint(games_data, roms_data):
    """games_data() / roms_data(): app.py's store-game cache and roms.catalog()."""
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
        return render_template(
            "home.html",
            play_next=_play_next(gc["games"], rc["cards"], rc["reps"], gc, rc, cfg),
            devices=DEVICES, itches=ITCHES, device=device, itch=itch,
            device_row=_row(DEVICES[device], gc["games"], rc["reps"], cfg, rng) if device else [],
            itch_row=_row(ITCHES[itch], gc["games"], rc["reps"], cfg, rng) if itch else [],
            toggle=_toggle, system_labels=roms.SYSTEM_LABELS,
        )

    return bp

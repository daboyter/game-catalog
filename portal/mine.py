"""
Personal play status and ratings in the portal — /my ("My games") and the
POST /my/set the detail pages' buttons send. Storage and keys are in
../my_games.py (shared with mcp_server.py).

annotate() puts the user's data on catalog items for one request:
    item["my"]            the my_games entry, or None
    item["f"]["mystatus"] {"In progress"} / {"Complete"} / {"Not marked"}
    item["f"]["myrating"] {"9"} … / {"Not rated"}
It runs per request rather than in the catalog caches, so a click shows at
once. Store games come from app.py's catalog, ROMs as roms.py's cards (one
per game and system).

Only what the user marks counts. Store playtime and ES-DE plays are NOT
used to guess what the user is playing (removed 2026-11-09): a store
account or an emulation box is often shared with other people, so
"played recently" can say more about them than about the user.
"""

import pathlib, sys
from flask import Blueprint, abort, redirect, render_template, request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import my_games                                          # noqa: E402  (game-catalog root)

MYSTATUS_LABELS = ["In progress", "Complete", "Not marked"]
MYRATING_LABELS = [str(n) for n in range(10, 0, -1)] + ["Not rated"]


def keys_for(item, kind):
    if kind == "game":
        return my_games.game_keys(item["igdb_id"], item["refs"])
    return my_games.rom_keys(item["system"], item["igdb_id"],
                             [m["rom_path"] for m in item.get("versions", [item])])


def annotate(items, kind):
    """Attach the user's status/rating to each item (see the module docstring)."""
    data = my_games.entries()
    for it in items:
        entry = my_games.find(keys_for(it, kind), data) if data else None
        it["my"] = entry
        status = (entry or {}).get("status")
        rating = (entry or {}).get("rating")
        it["f"]["mystatus"] = {my_games.STATUSES[status] if status else "Not marked"}
        it["f"]["myrating"] = {str(rating) if rating else "Not rated"}
    return items


LIKED_COMMON = 0.2    # labels on more than 20% of store games ("Action") say nothing about taste


def liked_labels(games, cards, min_rating):
    """
    Theme and curated-tag labels of everything rated min_rating or higher (for
    picks), minus labels so common they'd boost nearly every candidate.
    """
    out = set()
    for it in list(games) + list(cards):
        if ((it.get("my") or {}).get("rating") or 0) >= min_rating:
            out |= {l for l in it["labels"] if l[0] in ("theme", "tag")}
    if out and games:
        too_common = len(games) * LIKED_COMMON
        out = {l for l in out if sum(1 for g in games if l in g["labels"]) <= too_common}
    return frozenset(out)


def pick_cfg(cfg, games, rom_cards, rom_pool):
    """
    The settings for one page's pick rows, with the user's data applied:
    annotates every pool (so completed games can be skipped) and adds
    cfg["liked"] — labels of highly rated games — when "favor" is on.
    """
    annotate(games, "game")
    annotate(rom_cards, "rom")
    annotate([r for r in rom_pool if "versions" not in r], "rom")   # one-per-IGDB-game ROMs
    liked = liked_labels(games, rom_cards, cfg["favor_min_rating"]) if cfg["favor_rated"] else frozenset()
    return {**cfg, "liked": liked}


def make_blueprint(games_data, roms_data):
    """games_data() / roms_data(): the cached store-game and ROM catalogs."""
    bp = Blueprint("mine", __name__)

    def resolve(kind, item_id):
        """(item, title, detail URL, extra info for the file) for a game or ROM id."""
        if kind == "game":
            item = games_data()["by_id"].get(item_id)
            if not item:
                abort(404)
            return item, item["title"], f"/game/{item_id}", \
                {"igdb_id": item["igdb_id"], "refs": sorted(item["refs"])}
        if kind == "rom":
            item = roms_data()["card_of"].get(item_id)
            if not item:
                abort(404)
            return item, item["title"], f"/rom/{item_id}", \
                {"igdb_id": item["igdb_id"], "system": item["system"], "path": item["rom_path"]}
        abort(400)

    @bp.route("/my/set", methods=["POST"])
    def my_set():
        kind = request.form.get("kind", "")
        try:
            item_id = int(request.form.get("id", ""))
        except ValueError:
            abort(400)
        item, title, url, info = resolve(kind, item_id)
        changes = {}
        if "status" in request.form:
            status = request.form["status"]
            if status not in (*my_games.STATUSES, "none"):
                abort(400)
            changes["status"] = None if status == "none" else status
        if "rating" in request.form:
            rating = request.form["rating"]
            if rating == "clear":
                changes["rating"] = None
            elif rating.isdigit() and 1 <= int(rating) <= 10:
                changes["rating"] = int(rating)
            else:
                abort(400)
        if not changes:
            abort(400)
        my_games.update(keys_for(item, kind), kind=kind, title=title, **changes, **info)
        return redirect(f"{url}#my")       # always our own page: no open redirect

    @bp.route("/my")
    def my_page():
        games = annotate(list(games_data()["games"]), "game")
        cards = annotate([c for c in roms_data()["cards"]], "rom")
        items = [("game", g) for g in games] + [("rom", c) for c in cards]
        marked = [(k, it) for k, it in items if it["my"]]
        by_title = lambda x: x[1]["title_lower"]
        in_progress = sorted((x for x in marked if x[1]["my"].get("status") == "in_progress"),
                             key=lambda x: (-(x[1]["my"].get("rating") or 0), x[1]["title_lower"]))
        rated = sorted((x for x in marked if x[1]["my"].get("rating")),
                       key=lambda x: (-x[1]["my"]["rating"], x[1]["title_lower"]))
        complete = sorted((x for x in marked if x[1]["my"].get("status") == "complete"), key=by_title)
        # entries whose game/ROM isn't in the library any more (kept, never deleted)
        found = {id(it["my"]) for _, it in marked}
        missing = sorted((e for e in my_games.entries().values() if id(e) not in found),
                         key=lambda e: e.get("title", "").lower())
        from roms import SYSTEM_LABELS                   # here: roms.py imports this module
        return render_template("my.html", in_progress=in_progress, rated=rated, complete=complete,
                               system_labels=SYSTEM_LABELS,
                               missing=missing,
                               my_file=str(my_games.MY_FILE).replace(str(pathlib.Path.home()), "~"))

    return bp

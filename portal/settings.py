"""
Recommendation settings — /settings/recommendations, next to /scans.

The pick rows (recommend.py) read their knobs from a JSON file the portal
edits, so tuning them needs no code change or restart:
    ~/.local/share/game-catalog/settings/recommendations.json
(not the repo: it's edited from the portal; not ~/.config/game-catalog:
that's secrets). The file holds only what was changed; anything missing
falls back to DEFAULTS, which are the values in recommend.py — so deleting
the file ("Reset to defaults") gives exactly the built-in behavior.

current() re-reads the file only when its modification time changes, so a
save applies on the next page load in every portal worker. Saves are
validated, then written to a temp file and renamed over the old one, so a
reader never sees half a file.
"""

import json, os, pathlib, tempfile
from flask import Blueprint, redirect, render_template, request, url_for

import recommend

SETTINGS_FILE = pathlib.Path.home() / ".local/share/game-catalog/settings/recommendations.json"

# key → (label, help, type, default, min, max, step); order = order on the page
FIELDS = {
    "show_tangents":  ("Show Tangents", "Same-kind surprising matches on game and ROM pages",
                       bool, True, None, None, None),
    "show_retro":     ("Show Retro Picks", "Related ROMs on game pages",
                       bool, True, None, None, None),
    "show_modern":    ("Show Modern Picks", "Related store games on ROM pages",
                       bool, True, None, None, None),
    "tangents_picks": ("Tangents: how many", "Picks in the Tangents row",
                       int, recommend.PICKS, 1, 12, 1),
    "expected_picks": ("Tangents: how many are closer, more expected matches",
                       "Drawn from games that share a genre, without the surprise bonuses below; "
                       "the rest are the surprising kind",
                       int, recommend.EXPECTED_PICKS, 0, 12, 1),
    "retro_picks":    ("Retro Picks: how many", "Picks in the Retro Picks row",
                       int, recommend.RELATED_PICKS, 1, 16, 1),
    "modern_picks":   ("Modern Picks: how many", "Picks in the Modern Picks row",
                       int, recommend.RELATED_PICKS, 1, 16, 1),
    "genre_bonus":    ("How strongly Tangents favor a different genre",
                       "1 = no preference; 1.6 = a match from another genre counts 60% more",
                       float, recommend.GENRE_BONUS, 1.0, 4.0, 0.05),
    "era_bonus":      ("How strongly Tangents favor a different era",
                       "1 = no preference; 1.25 = a match from another era counts 25% more",
                       float, recommend.ERA_BONUS, 1.0, 4.0, 0.05),
    "era_gap":        ("Years apart that count as a different era", "Used by the era preference above",
                       int, recommend.ERA_GAP, 1, 60, 1),
    "favor_rated":    ("Favor games like ones I rated highly",
                       "Picks that share themes or curated tags with games you rated at least the "
                       "number below rank higher", bool, True, None, None, None),
    "favor_min_rating": ("\"Rated highly\" means my rating of at least",
                       "1–10, from the My rating buttons on game and ROM pages",
                       int, 8, 1, 10, 1),
    "skip_completed": ("Don't recommend games I've completed",
                       "Games marked Complete are left out of every pick row. Games marked "
                       "In progress are never left out", bool, True, None, None, None),
    "rare_max":       ("A shared trait is \"rare\" when at most this many games have it",
                       "Tangents need rare shared traits. Lower = stricter, odder matches; "
                       "higher = more, broader matches",
                       int, recommend.RARE_MAX, 5, 500, 1),
}
# Page sections: (heading, field keys)
SECTIONS = [
    ("Rows",                   ["show_tangents", "show_retro", "show_modern"]),
    ("Sizes and surprise",     ["tangents_picks", "expected_picks", "retro_picks", "modern_picks",
                                "genre_bonus", "era_bonus", "era_gap", "rare_max"]),
    ("My ratings and status",  ["favor_rated", "favor_min_rating", "skip_completed"]),
]
DEFAULTS = {k: f[3] for k, f in FIELDS.items()} | {
    "exclude": sorted(recommend.EXCLUDE_FROM_PICKS),
}

# Label kinds offered for "don't recommend", in page order: (kind, heading)
EXCLUDE_KINDS = [
    ("perspective", "Perspectives"),
    ("tag",         "Curated tags"),
    ("genre",       "Genres"),
    ("theme",       "Themes"),
    ("keyword",     "Keywords"),
]
KEYWORD_RESULTS = 60     # keyword search: matches shown

_state = {"mtime": None, "cfg": None}


def current():
    """The settings in effect: DEFAULTS overlaid with the file's values."""
    try:
        mtime = SETTINGS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    if _state["cfg"] is None or mtime != _state["mtime"]:
        saved = {}
        if mtime is not None:
            try:
                saved = json.loads(SETTINGS_FILE.read_text())
            except (OSError, ValueError):
                saved = {}                   # unreadable file: run on defaults, page shows them
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        cfg["exclude_set"] = {tuple(x) for x in cfg["exclude"]}
        _state.update(mtime=mtime, cfg=cfg)
    return _state["cfg"]


def parse_form(form):
    """(settings dict, [errors]) from the posted form; nothing is saved when there are errors."""
    out, errors = {}, []
    for key, (label, _, typ, default, lo, hi, _) in FIELDS.items():
        if typ is bool:
            out[key] = form.get(key) == "on"
            continue
        raw = form.get(key, "").strip()
        try:
            value = typ(raw)
        except ValueError:
            errors.append(f"{label}: \"{raw}\" isn't a number")
            continue
        if not lo <= value <= hi:
            errors.append(f"{label}: must be between {lo} and {hi}")
            continue
        out[key] = value
    if not errors and out["expected_picks"] > out["tangents_picks"]:
        errors.append("Tangents: the expected matches can't outnumber the whole row")
    kinds = {k for k, _ in EXCLUDE_KINDS}
    out["exclude"] = sorted({tuple(v.split("::", 1)) for v in form.getlist("exclude")
                             if "::" in v and v.split("::", 1)[0] in kinds})
    return out, errors


def save(cfg):
    """Write only the values that differ from DEFAULTS, atomically."""
    changed = {k: v for k, v in cfg.items() if k in DEFAULTS and v != DEFAULTS[k]}
    if "exclude" in changed:
        changed["exclude"] = [list(x) for x in changed["exclude"]]
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SETTINGS_FILE.parent, prefix=".recommendations.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(changed, f, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_FILE)


def reset():
    SETTINGS_FILE.unlink(missing_ok=True)


def label_counts(games, rom_games):
    """{(kind, value): (store games, ROM games)} over the labels items carry."""
    counts = {}
    for i, items in enumerate((games, rom_games)):
        for it in items:
            for label in it["labels"]:
                c = counts.setdefault(label, [0, 0])
                c[i] += 1
    return counts


def make_blueprint(games_data, roms_data):
    """games_data() / roms_data() → the portal's cached store-game and ROM catalogs."""
    bp = Blueprint("settings", __name__)

    @bp.route("/settings")
    def settings_home():
        return redirect(url_for("scans.scans"))

    @bp.route("/settings/recommendations", methods=["GET", "POST"])
    def recommendations():
        errors, notice = [], None
        cfg = current()
        if request.method == "POST":
            posted, errors = parse_form(request.form)
            if not errors:
                save(posted)
                return redirect(url_for("settings.recommendations", saved=1))
            cfg = dict(cfg) | posted           # show what was typed next to the errors
            cfg["exclude_set"] = set(posted["exclude"])
        elif request.args.get("saved"):
            notice = "Saved. The picks use the new settings from the next page you open."
        elif request.args.get("reset"):
            notice = "Back to the defaults."

        counts = label_counts(games_data()["games"], roms_data()["reps"])
        excluded = cfg["exclude_set"]
        kw = request.args.get("kw", "").strip().lower()
        sections = []
        for kind, heading in EXCLUDE_KINDS:
            values = [(v, n) for (k, v), n in counts.items() if k == kind and (k, v) not in excluded]
            if kind == "keyword":
                values = [x for x in values if kw and kw in x[0]] if kw else []
                values = sorted(values, key=lambda x: -(x[1][0] + x[1][1]))[:KEYWORD_RESULTS]
            else:
                values.sort(key=lambda x: x[0].lower())
            sections.append({"kind": kind, "heading": heading, "values": values})
        current_excl = sorted(((k, v, counts.get((k, v), (0, 0))) for k, v in excluded),
                              key=lambda x: ([k for k, _ in EXCLUDE_KINDS].index(x[0])
                                             if x[0] in dict(EXCLUDE_KINDS) else 99, x[1].lower()))
        return render_template("settings_recommendations.html",
            cfg=cfg, fields=FIELDS, defaults=DEFAULTS, errors=errors, notice=notice,
            bool_keys={k for k, f in FIELDS.items() if f[2] is bool}, sections_fields=SECTIONS,
            sections=sections, current_excl=current_excl, kind_headings=dict(EXCLUDE_KINDS),
            kw=kw, settings_file=str(SETTINGS_FILE).replace(str(pathlib.Path.home()), "~"),
        ), (400 if errors else 200)

    @bp.route("/settings/recommendations/reset", methods=["POST"])
    def recommendations_reset():
        reset()
        return redirect(url_for("settings.recommendations", reset=1))

    return bp

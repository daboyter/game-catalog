"""
Shared faceted-browsing helpers for the portal's grids (store games in
app.py, ES-DE ROMs in roms.py).

Each grid keeps its whole catalog in memory as a list of dicts. Every item
has an "f" dict mapping facet key → set of values, e.g.
    {"genre": {"RPG"}, "store": {"steam", "gog"}, ...}

Filters live in the URL as repeated params:
    /roms?system=snesna&genre=RPG&genre=Platformer&sort=rating
Within one facet, values are OR'd (RPG or Platformer); across facets they
are AND'd (… and SNES). Each facet's counts are computed with every OTHER
facet's filters applied, so a count is what you'd get by ticking it.
"""

import re
from collections import Counter
from urllib.parse import urlencode
from flask import request

SHOW_TOP = 8      # options shown per facet before "show more"
PER_PAGE = 200


# ── search ────────────────────────────────────────────────────────────────────
# The search box is a case-insensitive substring match on the title, with
# roman and arabic numerals treated alike: "final fantasy 7" finds "Final
# Fantasy VII", "civilization ii" finds "Civilization 2". Query and titles go
# through search_text(), which turns whole-word roman numerals into digits.
#   II–XX: converted wherever they're a whole word ("Part II", "II:", "IV -").
#   I, V, X: also ordinary words/letters ("I Am Bread", "BALL x PIT"), so only
#            converted after another word and at the end of the title or
#            before ":", "-", "(", "," ("Civilization V", "Final Fantasy X:").
_ROMAN = {r: n for n, r in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), 1)}
_MULTI_RE  = re.compile(r"\b(" + "|".join(sorted((r for r in _ROMAN if len(r) > 1), key=len, reverse=True))
                        + r")\b")
_SINGLE_RE = re.compile(r"(?<=\w )([ivx])(?=\s*(?:$|[:\-–(,]))")


def search_text(text):
    """Lowercased text with roman numerals as digits, for search matching."""
    s = (text or "").lower()
    s = _MULTI_RE.sub(lambda m: str(_ROMAN[m[1]]), s)
    return _SINGLE_RE.sub(lambda m: str(_ROMAN[m[1]]), s)


def search(items, q):
    """
    Items whose title contains the query, as typed (the old behavior) or with
    numerals normalized on both sides. Both, so "mega man x" still finds
    "Mega Man X2" although its normalized form is "mega man 10".
    """
    raw = q.strip().lower()
    if not raw:
        return items
    norm = search_text(raw)
    return [i for i in items if raw in i["title_lower"] or norm in i["search"]]


# ── URL helpers (filters live in the query string) ────────────────────────────

def params(drop=()):
    """Current query params as (key, value) pairs, minus page and any `drop` keys."""
    return [(k, v) for k, v in request.args.items(multi=True)
            if k != "page" and k not in drop and v != ""]


def url_for_params(pairs):
    return f"{request.path}?{urlencode(pairs)}" if pairs else request.path


def toggle_url(facet, value):
    pairs = params()
    if (facet, value) in pairs:
        pairs.remove((facet, value))
    else:
        pairs.append((facet, value))
    return url_for_params(pairs)


def page_url(page):
    return url_for_params(params() + [("page", page)])


def clear_url():
    """Drop every filter, keep the search text and sort."""
    return url_for_params([(k, v) for k, v in params() if k in ("q", "sort")])


# ── filtering, facets, chips, sorting, paging ─────────────────────────────────

def selected_filters(keys):
    """{facet: set(values)} for the given facet keys present in the URL."""
    sel = {k: set(request.args.getlist(k)) - {""} for k in keys}
    return {k: v for k, v in sel.items() if v}


def matches(item, selected, skip=None):
    for facet, values in selected.items():
        if facet != skip and not (item["f"][facet] & values):
            return False
    return True


def build_facets(base, selected, facets, label, order):
    """
    Sidebar data. facets: [(key, heading, open_by_default)];
    label(key, value) → display text; order(key, counts) → values in display order.
    """
    out = []
    for key, heading, default_open in facets:
        counts = Counter()
        for item in base:
            if matches(item, selected, skip=key):
                counts.update(item["f"][key])
        active = selected.get(key, set())
        for v in active:
            counts.setdefault(v, 0)        # keep ticked options visible even at 0
        options = [{"value": v, "label": label(key, v), "count": counts[v], "active": v in active}
                   for v in order(key, counts)]
        # ticked options first so they're never hidden under "show more"
        options.sort(key=lambda o: not o["active"])
        out.append({
            "key": key, "heading": heading, "open": default_open or bool(active),
            "n_active": len(active),
            "top": options[:SHOW_TOP], "more": options[SHOW_TOP:],
        })
    return out


def by_count(counts):
    """Default option order: most common first, then A–Z."""
    return sorted(counts, key=lambda v: (-counts[v], str(v).lower()))


def chips(selected, keys, headings, label):
    return [{"heading": headings[k], "label": label(k, v), "url": toggle_url(k, v)}
            for k in keys for v in sorted(selected.get(k, ()))]


def sort_items(items, sort, getters):
    """
    A–Z first, then by getters[sort] = (value getter, descending?).
    Items whose value is None always go last; ties stay A–Z (sorted() is
    stable, also with reverse=True).
    """
    items = sorted(items, key=lambda i: i["title_lower"])
    if sort not in getters:
        return items
    get, desc = getters[sort]
    have = [i for i in items if get(i) is not None]
    missing = [i for i in items if get(i) is None]
    return sorted(have, key=get, reverse=desc) + missing


def paginate(items):
    """(page_items, page, total_pages) for the ?page= param."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total_pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    return items[(page - 1) * PER_PAGE: page * PER_PAGE], page, total_pages

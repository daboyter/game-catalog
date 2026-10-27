"""
Recommendation rows for the detail pages.

"Tangents" (game → games, ROM → ROMs): 6 items of the same kind that share
surprisingly specific traits with this one, drawn at random each visit; one
of them is an "expected-ish" pick (see below).
Works on any list of items with "id", "year", "traits" and "f" (series,
developer, genre sets): store games from app.py, or one ROM per IGDB game
from roms.py. "Retro" / "Modern" picks are at the bottom (related()).

How a candidate is scored
-------------------------
Traits are a game's IGDB themes and keywords (game_tags kinds theme /
keyword), minus store/technical keyword noise (NOISE below). Curated tags
are left out: they're built from keywords, so they'd count twice.
Near-identical keywords ("surreal" / "surrealism") count once.
Each trait is weighted by its rarity in THIS library (inverse document
frequency): sharing "talking animals" (12 games) is worth far more than
sharing "Action" (600 games).

  score = sum of the shared traits' weights
          x 1.6  if the two games share no genre      (the "unexpected" part)
          x 1.25 if they're 15+ years apart

A candidate needs at least MIN_RARE shared traits that are rare (on at most
RARE_MAX games). Same series or same developer is skipped as too obvious.
Sparse games — fewer than MIN_RARE rare traits of their own, e.g. Agent A,
whose only usable keyword is "espionage" — would never qualify, so for them
one shared rare trait is enough when a theme or perspective is shared too.

From the TOP_N best, PICKS - EXPECTED_PICKS are drawn at random, weighted by
score², so a reload (or "Shuffle") shows a different mix while favoring the
best matches. The other EXPECTED_PICKS come from a second pool: candidates
sharing a genre, scored without the genre/era bonuses — the closer, more
traditional match that keeps the row from being all curveballs.

Filling the row
---------------
Strict matches are rare for games whose traits are one-offs (Aerial_Knight's
Never Yield: four rare keywords, but no game shares two of them) or that have
no IGDB traits at all (unmatched games, unmatched ROMs). Empty slots are
filled from looser tiers, in order, each drawn the same weighted way:
  2. one shared rare trait plus a shared theme or perspective     ("Both: …")
  3. generic similarity: shared genres, themes, modes, perspectives, series,
     developers (series and developer allowed here)                ("Similar: …")
  4. same genre and era (within ERA_NEAR years, or year unknown)   ("Also: …")

EXCLUDE_FROM_PICKS (below) removes items from every pick row on this page
and the Retro / Modern rows.

Settings
--------
The constants below are DEFAULTS. The portal's ⚙ → Recommendations page
(settings.py) overrides them without a code change: suggestions() and
related() take that page's settings dict as `cfg` (None = these defaults),
and exclusions are checked at pick time against each item's "labels", so a
change applies on the next page load.

The user's own data (mine.py) comes in through cfg too: candidates the user
marked Complete are skipped when cfg["skip_completed"] (never "In
progress" ones: those are often "forever" games they keep returning to),
and cfg["liked"] — themes and curated tags of games rated highly — raises a
candidate's score by LIKED_BOOST per shared label, up to LIKED_MAX labels.
"""

import math, random, re

PICKS     = 6
EXPECTED_PICKS = 1
TOP_N     = 30
MIN_RARE  = 2
RARE_MAX  = 60      # a trait on more games than this isn't "rare"
GENRE_BONUS = 1.6
ERA_BONUS   = 1.25
ERA_GAP     = 15    # years

TRAIT_KINDS = ("theme", "keyword")

ERA_NEAR = 5        # tier 4: "same era" = released within this many years
# tier 3 weights per shared label kind
GENERIC_KINDS = {"series": 2.0, "developer": 2.0, "theme": 1.5, "genre": 1.0,
                 "perspective": 1.0, "mode": 0.5}

# Personal preference: the user doesn't play side-scrollers (2026-10-23) or
# soulslikes (2026-10-24), so nothing with any of these labels is ever
# recommended — in Tangents, Retro Picks and Modern Picks, for games and ROMs
# alike. Browsing and filters are NOT affected. (kind, value) pairs as stored
# in game_tags / rom_tags; keyword values are lowercase. Deliberately not
# here: genre "Platformer" (includes 3D platformers), keyword "2d" (also
# top-down games), keyword "souls as currency" (not a soulslike).
# This is the DEFAULT list: change it on ⚙ → Recommendations in the portal
# (stored in settings/recommendations.json), not here.
EXCLUDE_FROM_PICKS = {
    # soulslikes
    ("tag",         "Soulslike"),
    ("keyword",     "soulslike"),
    ("keyword",     "souls-like"),
    # side-scrollers
    ("perspective", "Side view"),
    ("keyword",     "side-scrolling"),
    ("keyword",     "sidescroller"),
    ("keyword",     "2d platformer"),
    ("keyword",     "run and gun"),
    ("keyword",     "metroidvania"),
    ("keyword",     "puzzle platformer"),
    ("tag",         "Metroidvania"),
    ("tag",         "Puzzle platformer"),
}


LABEL_KINDS = ("genre", "theme", "tag", "perspective", "mode")

LIKED_BOOST = 0.2   # score × (1 + 0.2 per theme/tag shared with highly rated games)
LIKED_MAX   = 3     # … counting at most 3 of them (so at most × 1.6)


def _liked_boost(c, liked):
    return 1 + LIKED_BOOST * min(len(c["labels"] & liked), LIKED_MAX) if liked else 1


def _completed(c):
    return (c.get("my") or {}).get("status") == "complete"


def item_labels(f, keywords, extra=()):
    """
    An item's (kind, value) labels that exclusions are checked against: its
    facet sets for LABEL_KINDS, its raw IGDB keywords (noise included, so any
    keyword can be excluded), plus `extra` pairs (ROMs: IGDB genres, since
    their "genre" facet is ScreenScraper's).
    """
    return frozenset({(k, v) for k in LABEL_KINDS for v in f.get(k, ())}
                     | {("keyword", v) for v in keywords} | set(extra))


def _defaults():
    return {"tangents_picks": PICKS, "expected_picks": EXPECTED_PICKS, "rare_max": RARE_MAX,
            "genre_bonus": GENRE_BONUS, "era_bonus": ERA_BONUS, "era_gap": ERA_GAP,
            "exclude_set": EXCLUDE_FROM_PICKS}

# Keywords that describe the store, platform, tech or marketing rather than the
# game itself. Deliberately NOT here: odd-but-real traits like "you can pet the
# dog", "wilhelm scream" or "vending machine interaction" — those make the fun links.
NOISE = re.compile(
    r"steam|achievement|\bpax\b|gamescom|game awards|game critics|e3 \d|xbox|playstation|"
    r"dualshock|dualsense|controller|gamepad|digital distribution|bink|humble bundle|"
    r"crowd ?fund|kickstarter|greenlight|available on|previously on|\b4k\b|\bfps\b|\bhdr\b|"
    r"dolby|windows|onlive|\bgog\b|\bea app\b|games with gold|games on demand|cloud save|"
    r"retail|early access|pre-release|high definition|polygonal|^[23]d$|single-player only|"
    r"difficulty|auto-?sav|quick ?(save|load)|checkpoint|^health$|^attributes$|stat tracking|"
    r"leaderboard|high score|^sequel$|downloadable content|microtransaction|remote play|"
    r"motion blur|anti-cheat|text size|subtitle|colou?r ?blind|accessib|apple arcade|netflix|"
    r"utomik|stadia|\bluna\b|blacknut|gamersgate|freeware|free-to-play|\bdemo\b|"
    r"platform exclusive|unreal engine|unity engine|ray tracing|cross-?play|"
    r"online|multiplayer|co-?op|\bpvp\b|\bpve\b|split ?screen|"
    r"protagonist's name in the title|acronym titles|developer cameo|"
    r"^\d{4}'?s?$|^\d+-bit$|resolution|widescreen|frame ?rate|"
    r"\bsupport\b|customiz|controls|award|nominee|easter egg|collectible|collection log|"
    r"^weapons?$|^stamina$|^combat$|^exploration$|^dark$|^violent$|^gore$|"
    r"soundtrack|professional gaming|linear gameplay|realtime|real-time 3d|side quests|^camera$"
)

# Different keywords for the same idea; counted once (key → canonical key)
SYNONYMS = {
    "roguelite": "roguelike", "roguelitee": "roguelike",
    "pixelgraphic": "pixelart", "pixelartgame": "pixelart",
    "storydriven": "storyrich", "narrativedriven": "storyrich",
    "soulslike": "soulslike", "souslike": "soulslike",
}


def trait_set(game_tags):
    """(kind, value) pairs from a game's tags that count as traits."""
    return {(kind, value) for kind, value in game_tags
            if kind in TRAIT_KINDS and not (kind == "keyword" and NOISE.search(value))}


def build_weights(games):
    """({trait: idf weight} over the whole catalog, catalog size for the rarity cut-off)."""
    df = {}
    for g in games:
        for t in g["traits"]:
            df[t] = df.get(t, 0) + 1
    n = max(len(games), 1)
    return {t: math.log(n / c) for t, c in df.items() if c >= 2}, n


def _same_word(value):
    """Key that treats "surreal"/"surrealism", "zombie"/"zombies" as one trait."""
    k = re.sub(r"(ism|s)$", "", re.sub(r"[^a-z0-9]", "", value.lower()))
    return SYNONYMS.get(k, k)


def _display(trait):
    kind, value = trait
    return value[:1].upper() + value[1:] if kind == "keyword" else value


def _draw(pool, n, rng, taken):
    """Up to n items from the TOP_N of pool, weighted by score², skipping ids in taken."""
    pool = [x for x in sorted(pool, key=lambda x: -x[0]) if x[1]["id"] not in taken][:TOP_N]
    out = []
    while pool and len(out) < n:                # weighted draw without replacement
        i = rng.choices(range(len(pool)), weights=[x[0] ** 2 for x in pool])[0]
        out.append(pool.pop(i))
        taken.add(out[-1][1]["id"])
    return out


def _decade(year):
    return f"{year // 10 * 10}s" if year else None


def suggestions(game, games, weights, n, cfg=None, rng=random):
    """
    Up to cfg["tangents_picks"] {game, prefix, because, contrast} dicts for
    `game`, randomized. n: catalog size from build_weights().
    """
    cfg = cfg or _defaults()
    picks_n, expected_n = cfg["tangents_picks"], min(cfg["expected_picks"], cfg["tangents_picks"])
    exclude = cfg["exclude_set"]
    skip_done, liked = cfg.get("skip_completed"), cfg.get("liked")
    rare_cutoff = math.log(max(n, 1) / cfg["rare_max"])
    mine = game["traits"]
    f = game["f"]
    sparse = sum(1 for t in mine if weights.get(t, 0) >= rare_cutoff) < MIN_RARE
    surprising, expected, one_rare, generic, same_era = [], [], [], [], []
    for c in games:
        if c["id"] == game["id"] or (exclude and c["labels"] & exclude) or (skip_done and _completed(c)):
            continue
        cf = c["f"]
        boost = _liked_boost(c, liked)
        new_genre = bool(cf["genre"]) and not (cf["genre"] & f["genre"])
        far_apart = bool(game["year"] and c["year"] and abs(game["year"] - c["year"]) >= cfg["era_gap"])
        obvious = bool((cf["series"] & f["series"]) or (cf["developer"] & f["developer"]))

        shared, seen = [], set()
        for t in sorted((t for t in mine & c["traits"] if t in weights), key=lambda t: -weights[t]):
            if _same_word(t[1]) not in seen:          # rarest first; near-duplicates dropped
                seen.add(_same_word(t[1]))
                shared.append(t)
        rare = [t for t in shared if weights[t] >= rare_cutoff]
        score = sum(weights[t] for t in shared)
        if rare and not obvious:
            themes = [t for t in shared if t[0] == "theme" and t not in rare]
            views = sorted(cf["perspective"] & f["perspective"])
            if len(rare) >= MIN_RARE or (sparse and (themes or views)):
                because = [_display(t) for t in rare[:3]] if len(rare) >= MIN_RARE else \
                          [_display(rare[0])] + [_display(t) for t in themes[:1]] + views[:1]
                bonus = (cfg["genre_bonus"] if new_genre else 1) * (cfg["era_bonus"] if far_apart else 1)
                surprising.append((score * bonus * boost, c, "Both", because, new_genre, far_apart))
                if cf["genre"] & f["genre"]:
                    expected.append((score * boost, c, "Both", because, False, False))
                continue
            if themes or views:                      # tier 2
                because = [_display(rare[0])] + [_display(t) for t in themes[:1]] + views[:1]
                one_rare.append((score * boost, c, "Both", because, new_genre, far_apart))
                continue

        # tier 3: generic similarity (labels, series/developer welcome)
        common = [(k, v) for k in GENERIC_KINDS for v in sorted(cf.get(k, set()) & f.get(k, set()))]
        gscore = sum(GENERIC_KINDS[k] for k, _ in common)
        if common and any(k != "mode" for k, _ in common) and gscore >= 2:
            generic.append((gscore * boost, c, "Similar", [v for _, v in common[:3]], False, False))
        elif cf["genre"] & f["genre"] and (not game["year"] or not c["year"]
                                           or abs(game["year"] - c["year"]) <= ERA_NEAR):
            genre = sorted(cf["genre"] & f["genre"])[0]
            era = _decade(c["year"])
            same_era.append((boost, c, "Also", [genre] + ([era] if era else []), False, False))

    taken = set()
    picks = _draw(surprising, picks_n - expected_n, rng, taken)
    extra = _draw(expected, expected_n, rng, taken)
    extra += _draw(surprising, expected_n - len(extra), rng, taken)
    for x in extra:                             # anywhere in the row, not always last
        picks.insert(rng.randint(0, len(picks)), x)
    for tier in (one_rare, generic, same_era):  # fill what's left, closest tier first
        picks += _draw(tier, picks_n - len(picks), rng, taken)

    out = []
    for score, c, prefix, because, new_genre, far_apart in picks:
        contrast = []
        if new_genre:
            genre = sorted(c["f"]["genre"] - game["f"]["genre"])[0]
            contrast.append(f"{'an' if genre[0] in 'AEIOU' else 'a'} {genre} game")
        if far_apart:
            contrast.append(f"from {c['year']}")
        out.append({"game": c, "prefix": prefix, "because": because, "contrast": " ".join(contrast)})
    return out


# ── Retro / Modern Picks ──────────────────────────────────────────────────────
# "Retro Picks" (game page → ROMs) and "Modern Picks" (ROM page → store games):
# the traditional kind of related, in this order:
#   1. the same game, or its remake / remaster / port / original (IGDB relations,
#      passed in as `labels`)
#   2. same series (IGDB franchise or collection)
#   3. shared curated tags, themes, keywords, perspectives and genres, each
#      weighted by its rarity in the pool being picked from (a shared
#      "Metroidvania" tag beats a shared "Action"; "farming" is what ties
#      Stardew Valley to Harvest Moon)
# Kinds weigh tag 3 : theme 2 : keyword 1.5 : perspective 1.5 : genre 1
# (perspective keeps side-scrollers with side-scrollers; keywords minus NOISE,
# as for traits), and a pick from step 3 needs at least MIN_SHARED shared
# labels, not all of them genres. Only the KEYWORD_MAX rarest shared keywords
# count, so games with hundreds of keywords (BioShock) don't outscore a closer
# match with a few (Super Metroid → Axiom Verge); a keyword that repeats a tag
# ("crafting" / Crafting) counts once. At most SERIES_MAX_PICKS
# come from steps 1–2, so a big series ("Final Fantasy") leaves room for the
# rest. Deterministic: the same page always shows the same picks.
#
# Items carry a "rel" dict: {"series", "theme", "tag", "keyword", "perspective",
# "genre"} → set of values, normalized the same way for games and ROMs
# (enrich_details.py); rel_sets() builds it.

RELATED_PICKS    = 8
SERIES_MAX_PICKS = 6
MIN_SHARED       = 2
KEYWORD_MAX      = 3
REL_KINDS        = {"tag": 3.0, "theme": 2.0, "keyword": 1.5, "perspective": 1.5, "genre": 1.0}


def rel_sets(f, traits):
    """An item's "rel" dict from its facet sets and its traits (for the keywords)."""
    return {"series": f["series"], "tag": f["tag"], "theme": f["theme"], "genre": f["genre"],
            "perspective": f["perspective"], "keyword": {v for kind, v in traits if kind == "keyword"}}


def build_rel_weights(items):
    """{(kind, value): idf} over one pool (store games, or one ROM per IGDB game)."""
    df = {}
    for it in items:
        for kind in REL_KINDS:
            for v in it["rel"][kind]:
                df[(kind, v)] = df.get((kind, v), 0) + 1
    n = max(len(items), 1)
    return {t: math.log(n / c) + 0.1 for t, c in df.items()}   # +0.1: even "Action" counts a little


def related(item, pool, weights, labels=None, limit=RELATED_PICKS, exclude=EXCLUDE_FROM_PICKS,
            skip_completed=False, liked=frozenset()):
    """
    Up to `limit` {game, because, label} dicts: `item`'s most related items in
    `pool`. skip_completed / liked: the user's data, as for suggestions().
    """
    labels = labels or {}
    mine = item["rel"]
    scored = []
    for c in pool:
        if (exclude and c["labels"] & exclude) or (skip_completed and _completed(c)):
            continue
        label = labels.get(c["id"])
        series = sorted(mine["series"] & c["rel"]["series"])
        shared, seen, n_kw = [], set(), 0
        for t in sorted(((k, v) for k in REL_KINDS for v in mine[k] & c["rel"][k]),
                        key=lambda t: -REL_KINDS[t[0]] * weights.get(t, 0)):
            if _same_word(t[1]) in seen or (t[0] == "keyword" and n_kw >= KEYWORD_MAX):
                continue
            seen.add(_same_word(t[1]))
            n_kw += t[0] == "keyword"
            shared.append(t)
        if not (label or series) and (len(shared) < MIN_SHARED
                                      or all(k == "genre" for k, _ in shared)):
            continue
        score = sum(REL_KINDS[k] * weights.get((k, v), 0) for k, v in shared) * _liked_boost(c, liked)
        tier = 0 if label else 1 if series else 2
        scored.append((tier, -score, -(c.get("rating") or 0), c["title_lower"], c, label, series, shared))
    scored.sort(key=lambda s: s[:4])

    close = [s for s in scored if s[0] < 2]
    rest = [s for s in scored if s[0] == 2]
    picks = close[:SERIES_MAX_PICKS] + rest
    picks = (picks + close[SERIES_MAX_PICKS:])[:limit]   # few others → more of the series
    picks.sort(key=lambda s: s[:4])

    out = []
    for tier, _, _, _, c, label, series, shared in picks:
        because = [_display(t) for t in shared if t[1] not in series][:3]
        if series and not label:
            label = f"Series: {series[0]}"
        out.append({"game": c, "label": label, "because": because})
    return out

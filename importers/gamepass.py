#!/usr/bin/env python3
"""
Xbox Game Pass importer — fetches the full PC Game Pass catalog using
Microsoft's public (unauthenticated) catalog endpoints.

No OAuth, no app registration required.

Two-step process:
  Step 1. Catalog endpoint → list of Microsoft Store product IDs (9XXXXXXXX)
  Step 2. Products endpoint → title, description, cover art, categories
          (batched 20 IDs per request to stay within URL length limits)

Cache:
  ~/.local/share/game-catalog/cache/gamepass_catalog.json  (TTL 24h)
  → rebuilt from the products endpoint on first run or when stale

Platforms (ownership.platforms, comma-separated):
  pc       every entry (this is the PC Game Pass list)
  cloud    also in the Game Pass cloud-streaming list: playable through Xbox
           app cloud gaming (which streams the Xbox console version). Not the
           same as DroidDeck on the Odin 2, which installs PC packages; see
           the "Game Pass on" note in portal/app.py.
  console  also in the console Game Pass list
The cloud/console lists often use the console product's id rather than the
PC one, so a PC title counts as cloud/console if its id OR its title matches
(title compared with ™/®, "(Windows)", "PC Edition", "Edition" etc. removed).
As of 2026-10-22: 424 of 516 PC titles are cloud-streamable; the other 92
(Call of Duty "(Windows)" editions, Diablo, Anno 1800, older Assassin's
Creed and Fallout, …) are Windows-only and mostly use other PC launchers.

The imported rows use store='gamepass', store_id=<ProductId (9XXXXXXXX)>,
kind='subscription'. Xodus Nexus uses the same storeId format, so its 3
installed entries will be updated in place by this importer on conflict.

Usage:
    ~/game-catalog-venv/bin/python3 importers/gamepass.py [--force-refresh]
"""

import argparse, json, pathlib, re, sys, time, unicodedata, urllib.request, urllib.parse
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, upsert_game

# ── endpoints ─────────────────────────────────────────────────────────────────

# PC Game Pass catalog (returns first element = metadata, rest = {"id": "9XXXXXXXX"})
CATALOG_URL = (
    "https://catalog.gamepass.com/sigls/v2"
    "?id=fdd9e2a7-0fee-49f6-ad69-4354098401ff"
    "&language=en-us&market=US"
)

# Other Game Pass lists, used only to mark which PC titles are also on them
CLOUD_URL   = CATALOG_URL.replace("fdd9e2a7-0fee-49f6-ad69-4354098401ff",
                                  "29a81209-df6f-41fd-a528-2ae6b91f719c")
CONSOLE_URL = CATALOG_URL.replace("fdd9e2a7-0fee-49f6-ad69-4354098401ff",
                                  "f6f1f99f-9b49-4ccd-b3bf-4d9767a77f5e")

# Bulk product detail resolver (max 20 IDs per call in bigIds)
PRODUCTS_URL = (
    "https://displaycatalog.mp.microsoft.com/v7.0/products"
    "?bigIds={ids}&market=US&languages=en-us&MS-CV=0"
)

BATCH_SIZE  = 20
REQUEST_DELAY = 0.15   # seconds between batches (be polite; no documented rate limit)
CACHE_TTL   = 24 * 3600  # 24 hours

CACHE_FILE = (
    pathlib.Path.home()
    / ".local/share/game-catalog/cache/gamepass_catalog.json"
)

HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _image_url(images: list, *purposes: str) -> str | None:
    """Return the first image URL that matches one of the given purposes."""
    for purpose in purposes:
        for img in images:
            if img.get("ImagePurpose") == purpose:
                uri = img.get("Uri", "")
                if uri:
                    return ("https:" + uri) if uri.startswith("//") else uri
    return None


def _sigl_ids(url: str) -> list[str]:
    """Product ids from a catalog.gamepass.com list (first element is metadata)."""
    return [e["id"] for e in _get(url)[1:] if "id" in e]


def _product_titles(ids: list[str]) -> dict[str, str]:
    """{product id: title}, resolved 20 at a time."""
    titles = {}
    for i in range(0, len(ids), BATCH_SIZE):
        try:
            data = _get(PRODUCTS_URL.format(ids=",".join(ids[i : i + BATCH_SIZE])))
        except Exception as e:
            print(f"  [gamepass] title batch {i//BATCH_SIZE + 1} error: {e} — skipping")
            continue
        for p in data.get("Products", []):
            titles[p["ProductId"]] = p.get("LocalizedProperties", [{}])[0].get("ProductTitle", "")
        time.sleep(REQUEST_DELAY)
    return titles


def _title_key(title: str) -> str:
    """Loose title key for matching a PC product to its console/cloud product."""
    t = unicodedata.normalize("NFKD", title)
    t = re.sub(r"[™®©]", "", t).lower()
    t = re.sub(r"\b(for windows|windows 10|windows|pc|xbox series x\|s|xbox one|standard edition|edition)\b", "", t)
    return re.sub(r"[^a-z0-9]", "", t)


def _other_list(url: str, label: str) -> tuple[set[str], set[str]]:
    """(product ids, title keys) of another Game Pass list."""
    ids = _sigl_ids(url)
    keys = {_title_key(t) for t in _product_titles(ids).values() if t}
    print(f"  [gamepass] {len(ids)} titles in the {label} list")
    return set(ids), keys


# ── fetch catalog ─────────────────────────────────────────────────────────────

def fetch_catalog(force: bool = False) -> list[dict]:
    """
    Return a list of product dicts, each with at minimum:
      {id, title, publisher, developer, description, cover_url, square_url, categories}
    Uses a 24-hour local cache.
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        cached = json.loads(CACHE_FILE.read_text())
        # caches written before platforms were tracked are refetched
        if age < CACHE_TTL and cached and "platforms" in cached[0]:
            print(f"  [gamepass] using cache ({int(age / 3600)}h old)")
            return cached

    # Step 1: get the list of product IDs
    print("  [gamepass] fetching PC Game Pass catalog…")
    raw    = _get(CATALOG_URL)
    # First entry is catalog metadata; rest are {"id": "9XXXXXXXX"}
    entries = raw[1:]  # skip the metadata entry
    ids     = [e["id"] for e in entries if "id" in e]
    print(f"  [gamepass] {len(ids)} product IDs in catalog")

    # Step 2: batch-resolve to titles
    products = []
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        url   = PRODUCTS_URL.format(ids=",".join(batch))
        try:
            data = _get(url)
        except Exception as e:
            print(f"  [gamepass] batch {i//BATCH_SIZE + 1} error: {e} — skipping")
            continue

        for p in data.get("Products", []):
            lp         = p.get("LocalizedProperties", [{}])[0]
            images     = lp.get("Images", [])
            cover_url  = _image_url(images, "BoxArt", "Poster", "BrandedKeyArt")
            square_url = _image_url(images, "Poster", "Logo", "SuperHeroArt")
            categories = p.get("Properties", {}).get("Categories", [])
            desc       = lp.get("ShortDescription") or lp.get("ProductDescription") or ""
            # Truncate very long descriptions to something DB-friendly
            if desc and len(desc) > 1000:
                desc = desc[:997] + "…"

            products.append({
                "id":          p["ProductId"],
                "title":       lp.get("ProductTitle", ""),
                "publisher":   lp.get("PublisherName", ""),
                "developer":   lp.get("DeveloperName", ""),
                "description": desc,
                "cover_url":   cover_url,
                "square_url":  square_url,
                "categories":  categories,
            })

        if i + BATCH_SIZE < len(ids):
            time.sleep(REQUEST_DELAY)

        if (i // BATCH_SIZE + 1) % 5 == 0:
            print(f"  [gamepass] resolved {len(products)} / {len(ids)}…")

    # Filter out entries without a title (API hiccups)
    products = [p for p in products if p["title"]]
    print(f"  [gamepass] {len(products)} games resolved")

    # Step 3: which of them can also be streamed / played on console
    others = {"cloud": _other_list(CLOUD_URL, "cloud"), "console": _other_list(CONSOLE_URL, "console")}
    for p in products:
        key = _title_key(p["title"])
        p["platforms"] = ["pc"] + [name for name, (ids, keys) in others.items()
                                   if p["id"] in ids or key in keys]
    n_cloud = sum("cloud" in p["platforms"] for p in products)
    print(f"  [gamepass] {n_cloud} cloud-streamable, {len(products) - n_cloud} Windows-only")

    CACHE_FILE.write_text(json.dumps(products))
    return products


# ── import ────────────────────────────────────────────────────────────────────

def run(force: bool = False):
    products = fetch_catalog(force)

    new_games  = 0
    upserted_o = 0

    with open_db() as db:
        for p in products:
            store_id  = p["id"]
            title     = p["title"]
            genres    = p["categories"]

            game_id, created = upsert_game(
                db, title,
                genres_json=json.dumps(genres) if genres else None
            )
            if created:
                new_games += 1

            db.execute("""
                INSERT INTO ownership
                    (game_id, store, store_id, store_title, kind, is_installed,
                     art_cover_url, art_square_url, platforms, raw_json)
                VALUES (?, 'gamepass', ?, ?, 'subscription', 0, ?, ?, ?, ?)
                ON CONFLICT (store, store_id) DO UPDATE SET
                    store_title    = excluded.store_title,
                    art_cover_url  = excluded.art_cover_url,
                    art_square_url = excluded.art_square_url,
                    platforms      = excluded.platforms,
                    raw_json       = excluded.raw_json,
                    imported_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """, (game_id, store_id, title, p["cover_url"], p["square_url"],
                  ",".join(p["platforms"]),
                  json.dumps({"id": store_id, "title": title,
                              "publisher": p["publisher"],
                              "developer": p["developer"]})))
            upserted_o += 1

        db.commit()

    print(f"  [gamepass] done: {new_games} new games, {upserted_o} ownership rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true",
                        help="Ignore 24h cache and re-fetch from Microsoft")
    args = parser.parse_args()
    run(force=args.force_refresh)

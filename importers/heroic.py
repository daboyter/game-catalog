#!/usr/bin/env python3
"""
Heroic importer — reads Epic, GOG, and Amazon library caches and upserts
into the game catalog DB.

Sources:
  Epic   — legendaryConfig/legendary/metadata/*.json  (one file per game;
            run `legendary list-games -q` headlessly before this to refresh)
  GOG    — store_cache/gog_library.json
  Amazon — nile_config/nile/library.json

GOG/Amazon data is only updated when Heroic's GUI opens; a warning is printed
if the files are > 1 week old.

Usage:
    ~/game-catalog-venv/bin/python3 importers/heroic.py
"""

import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, upsert_game, load_hidden, drop_hidden

HEROIC_CONFIG = (
    pathlib.Path.home()
    / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
)

EPIC_META_DIR = HEROIC_CONFIG / "legendaryConfig/legendary/metadata"
GOG_LIB       = HEROIC_CONFIG / "store_cache/gog_library.json"
AMAZON_LIB    = HEROIC_CONFIG / "store_cache/nile_library.json"

STALE_WARN_SECS = 7 * 24 * 3600

# (store, store_id) pairs from hidden-games.tsv — never imported
HIDDEN = load_hidden()


def _check_path(path: pathlib.Path, label: str) -> bool:
    if not path.exists():
        print(f"  [heroic/{label}] WARNING: {path} not found — skipping")
        return False
    age = time.time() - path.stat().st_mtime
    if age > STALE_WARN_SECS:
        print(f"  [heroic/{label}] WARNING: cache is {int(age // 86400)}d old"
              " — open Heroic to refresh GOG/Amazon")
    return True


# ── Epic ──────────────────────────────────────────────────────────────────────

def import_epic(db):
    if not EPIC_META_DIR.exists():
        print(f"  [heroic/epic] WARNING: {EPIC_META_DIR} not found — skipping")
        return

    files = sorted(EPIC_META_DIR.glob("*.json"))
    print(f"  [heroic/epic] {len(files)} metadata files")
    new_games = upserted = 0

    for fpath in files:
        try:
            data = json.loads(fpath.read_text())
        except Exception:
            continue

        meta     = data.get("metadata", {})
        title    = meta.get("title", "") or data.get("app_name", fpath.stem)
        app_name = data.get("app_name", fpath.stem)
        if not title or not app_name:
            continue

        # Skip DLC, add-ons, and packs — only import base games.
        # legendary marks these with mainGameItem or by categories containing "DLC"/"Add-On".
        categories = [c.get("path", "") for c in meta.get("categories", [])]
        if any("dlc" in c.lower() or "addon" in c.lower() or "add_on" in c.lower()
               for c in categories):
            continue
        # Also skip if it's a "maingameitem" child (DLC that references a parent)
        if meta.get("mainGameItem"):
            continue
        if ("epic", app_name) in HIDDEN:
            continue

        # All Epic claims are free_claim (the weekly/promo games)
        kind = "free_claim"

        # Cover art from keyImages array
        cover_url = square_url = None
        for img in meta.get("keyImages", []):
            t = img.get("type", "")
            if t == "DieselGameBox" and not cover_url:
                cover_url = img.get("url")
            if t == "DieselGameBoxTall" and not square_url:
                square_url = img.get("url")
        cover_url = cover_url or square_url

        game_id, created = upsert_game(db, title)
        if created:
            new_games += 1

        db.execute("""
            INSERT INTO ownership
                (game_id, store, store_id, store_title, kind, is_installed,
                 art_cover_url, art_square_url, raw_json)
            VALUES (?, 'epic', ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT (store, store_id) DO UPDATE SET
                store_title    = excluded.store_title,
                art_cover_url  = excluded.art_cover_url,
                art_square_url = excluded.art_square_url,
                raw_json       = excluded.raw_json,
                imported_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (game_id, app_name, title, kind, cover_url, square_url,
              json.dumps({"app_name": app_name, "title": title,
                          "developer": meta.get("developer", "")})))
        upserted += 1

    db.commit()
    print(f"  [heroic/epic] done: {new_games} new games, {upserted} ownership rows")


# ── GOG ───────────────────────────────────────────────────────────────────────

def import_gog(db):
    if not _check_path(GOG_LIB, "gog"):
        return

    raw   = json.loads(GOG_LIB.read_text())
    games = raw if isinstance(raw, list) else raw.get("games", raw.get("library", []))
    print(f"  [heroic/gog] {len(games)} raw entries")
    new_games = upserted = 0

    for g in games:
        if g.get("runner") != "gog":
            continue
        app_name = str(g.get("app_name", ""))
        title    = g.get("title", "")
        if not title or not app_name or app_name == "1":
            continue
        if "redistributable" in title.lower():
            continue
        if ("gog", app_name) in HIDDEN:
            continue

        extra     = g.get("extra") or {}
        genres    = extra.get("genres", [])
        cover_url = g.get("art_cover")
        sq_url    = g.get("art_square")
        installed = 1 if g.get("is_installed") else 0

        game_id, created = upsert_game(
            db, title,
            genres_json=json.dumps(genres) if genres else None
        )
        if created:
            new_games += 1

        db.execute("""
            INSERT INTO ownership
                (game_id, store, store_id, store_title, kind, is_installed,
                 art_cover_url, art_square_url, raw_json)
            VALUES (?, 'gog', ?, ?, 'purchased', ?, ?, ?, ?)
            ON CONFLICT (store, store_id) DO UPDATE SET
                store_title    = excluded.store_title,
                is_installed   = excluded.is_installed,
                art_cover_url  = excluded.art_cover_url,
                art_square_url = excluded.art_square_url,
                raw_json       = excluded.raw_json,
                imported_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (game_id, app_name, title, installed, cover_url, sq_url,
              json.dumps({"app_name": app_name, "title": title})))
        upserted += 1

    db.commit()
    print(f"  [heroic/gog] done: {new_games} new games, {upserted} ownership rows")


# ── Amazon ────────────────────────────────────────────────────────────────────

def import_amazon(db):
    if not _check_path(AMAZON_LIB, "amazon"):
        return

    raw   = json.loads(AMAZON_LIB.read_text())
    games = raw if isinstance(raw, list) else raw.get("library", raw.get("games", []))
    print(f"  [heroic/amazon] {len(games)} raw entries")
    new_games = upserted = 0

    for g in games:
        if g.get("runner") != "nile":
            continue
        app_name = str(g.get("app_name", ""))
        title    = g.get("title", "")
        if not title or not app_name or ("amazon", app_name) in HIDDEN:
            continue

        extra     = g.get("extra") or {}
        genres    = extra.get("genres", [])
        cover_url = g.get("art_cover")
        sq_url    = g.get("art_square")
        installed = 1 if g.get("is_installed") else 0

        game_id, created = upsert_game(
            db, title,
            genres_json=json.dumps(genres) if genres else None
        )
        if created:
            new_games += 1

        db.execute("""
            INSERT INTO ownership
                (game_id, store, store_id, store_title, kind, is_installed,
                 art_cover_url, art_square_url, raw_json)
            VALUES (?, 'amazon', ?, ?, 'free_claim', ?, ?, ?, ?)
            ON CONFLICT (store, store_id) DO UPDATE SET
                store_title    = excluded.store_title,
                is_installed   = excluded.is_installed,
                art_cover_url  = excluded.art_cover_url,
                art_square_url = excluded.art_square_url,
                raw_json       = excluded.raw_json,
                imported_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (game_id, app_name, title, installed, cover_url, sq_url,
              json.dumps({"app_name": app_name, "title": title})))
        upserted += 1

    db.commit()
    print(f"  [heroic/amazon] done: {new_games} new games, {upserted} ownership rows")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with open_db() as db:
        import_epic(db)
        import_gog(db)
        import_amazon(db)
        # hidden-games.tsv: delete rows imported before they were listed
        # (and any from stores whose importers don't skip them)
        dropped = drop_hidden(db, HIDDEN)
        db.commit()
        print(f"  [heroic] hidden-games.tsv: {len(HIDDEN)} entries, {dropped} games removed")

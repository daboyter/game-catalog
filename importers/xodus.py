#!/usr/bin/env python3
"""
Xodus Nexus importer — reads ~/.config/xodus-nexus/launcher-state.json.

IMPORTANT LIMITATION: Xodus only records *installed* Game Pass titles.
This importer will only ever see the games currently downloaded via Xodus.
It cannot enumerate the full Game Pass catalog. A warning is printed on every run.

Usage:
    ~/game-catalog-venv/bin/python3 importers/xodus.py
"""

import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, upsert_game

STATE_FILE = pathlib.Path.home() / ".config/xodus-nexus/launcher-state.json"


def run(db):
    print("  [xodus] WARNING: only installed Game Pass titles are tracked; "
          "full catalog requires Xbox OAuth (not implemented)")

    if not STATE_FILE.exists():
        print(f"  [xodus] {STATE_FILE} not found — skipping")
        return

    raw   = json.loads(STATE_FILE.read_text())
    games = raw.get("games", raw if isinstance(raw, list) else [])
    print(f"  [xodus] {len(games)} installed Game Pass games")

    new_games = upserted = 0
    for g in games:
        title    = g.get("title", "")
        store_id = g.get("storeId") or g.get("id", "")
        if not title or not store_id:
            continue

        # coverDataUrl is a data: URI — skip it (too large for the DB)
        cover_url = g.get("coverDataUrl", None)
        if cover_url and cover_url.startswith("data:"):
            cover_url = None

        installed = 1 if g.get("installState") == "ready" else 0

        game_id, created = upsert_game(db, title)
        if created:
            new_games += 1

        db.execute("""
            INSERT INTO ownership
                (game_id, store, store_id, store_title, kind, is_installed, raw_json)
            VALUES (?, 'gamepass', ?, ?, 'subscription', ?, ?)
            ON CONFLICT (store, store_id) DO UPDATE SET
                store_title  = excluded.store_title,
                is_installed = excluded.is_installed,
                raw_json     = excluded.raw_json,
                imported_at  = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (game_id, store_id, title, installed,
              json.dumps({"storeId": store_id, "title": title,
                          "publisher": g.get("publisher", "")})))
        upserted += 1

    db.commit()
    print(f"  [xodus] done: {new_games} new games, {upserted} ownership rows")


if __name__ == "__main__":
    with open_db() as db:
        run(db)

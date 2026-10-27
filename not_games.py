#!/usr/bin/env python3
"""
Hide store entries that aren't games (not-games.tsv) — run by refresh.sh
after the importers.

Matching ownership rows get kind = 'launcher', the kind every query already
treats as "not a game" (the Lutris launchers). Rows this script hid earlier
whose line was removed get their store's usual kind back (DEFAULT_KIND);
Lutris rows are left alone, since the Lutris importer sets their kind itself.

Usage:
    ~/game-catalog-venv/bin/python3 not_games.py
"""

import fnmatch, pathlib
from _db import open_db, list_file

LIST_FILE = list_file("not-games.tsv")
# ownership.kind each importer gives new rows (see importers/*.py)
DEFAULT_KIND = {"steam": "purchased", "gog": "purchased", "epic": "free_claim",
                "amazon": "free_claim", "gamepass": "subscription",
                "battlenet": "library", "ea": "library", "ubisoft": "library"}


def load_patterns():
    patterns = []
    for lineno, line in enumerate(LIST_FILE.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#") or line.startswith("store\t"):
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            print(f"  [not-games] not-games.tsv:{lineno}: expected store<TAB>store_id — skipping")
            continue
        patterns.append((cols[0].strip(), cols[1].strip()))
    return patterns


def run(db):
    patterns = load_patterns()
    hidden = restored = 0
    for r in db.execute("SELECT id, store, store_id, store_title, kind FROM ownership "
                        "WHERE store != 'lutris'").fetchall():
        listed = any(r["store"] == s and fnmatch.fnmatchcase(r["store_id"], pat) for s, pat in patterns)
        if listed and r["kind"] != "launcher":
            db.execute("UPDATE ownership SET kind = 'launcher' WHERE id = ?", (r["id"],))
            print(f"  [not-games] hiding {r['store']}:{r['store_id']} {r['store_title']!r}")
            hidden += 1
        elif not listed and r["kind"] == "launcher" and r["store"] in DEFAULT_KIND:
            db.execute("UPDATE ownership SET kind = ? WHERE id = ?", (DEFAULT_KIND[r["store"]], r["id"]))
            print(f"  [not-games] restoring {r['store']}:{r['store_id']} {r['store_title']!r}")
            restored += 1
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM ownership WHERE kind = 'launcher' AND store != 'lutris'").fetchone()[0]
    print(f"[not-games] done: {hidden} hidden, {restored} restored, {total} store entries hidden in all")


if __name__ == "__main__":
    with open_db() as db:
        run(db)

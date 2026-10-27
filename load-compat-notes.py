#!/usr/bin/env python3
"""
Load compat-notes.tsv into the compat_notes table.

THIS SCRIPT WIPES compat_notes AND REBUILDS IT FROM THE TSV.
The TSV is the source of truth. The database table is a queryable copy.
Run this after editing compat-notes.tsv.

Key resolution order per row:
  1. igdb_id (integer) — stable across DB rebuilds; preferred once available
  2. store_ref (e.g. "steam:400" or "gog:1207663233") — lookup via ownership table
  Both blank → skip with warning.

Usage:
    ~/game-catalog-venv/bin/python3 load-compat-notes.py [--tsv path/to/file.tsv]
"""

import argparse, pathlib, sqlite3, sys
from _db import open_db, list_file

DEFAULT_TSV = list_file("compat-notes.tsv")

VALID_STATUSES = {"works", "partial", "broken", "untested"}
VALID_DEVICES  = {"odin2", "bazzite", "mac", "steamdeck"}


def resolve_game_id(db: sqlite3.Connection, igdb_id: str, store_ref: str) -> int | None:
    """
    Return games.id for the row, or None if not found.
    Tries igdb_id first, then store_ref (store:store_id lookup in ownership).
    """
    if igdb_id:
        try:
            row = db.execute(
                "SELECT id FROM games WHERE igdb_id = ?", (int(igdb_id),)
            ).fetchone()
            if row:
                return row[0]
        except ValueError:
            pass

    if store_ref and ":" in store_ref:
        store, _, store_id = store_ref.partition(":")
        row = db.execute(
            "SELECT game_id FROM ownership WHERE store = ? AND store_id = ?",
            (store.strip(), store_id.strip())
        ).fetchone()
        if row:
            return row[0]

    return None


def run(db: sqlite3.Connection, tsv_path: pathlib.Path):
    if not tsv_path.exists():
        sys.exit(f"ERROR: {tsv_path} not found")

    lines = tsv_path.read_text().splitlines()
    rows  = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts[0].strip() in ("igdb_id", "# igdb_id"):
            continue  # header row
        rows.append(parts)

    print(f"[compat-notes] wipe and rebuild — {len(rows)} rows to process")

    # Wipe existing rows (rebuild from TSV)
    db.execute("DELETE FROM compat_notes")

    upserted = skipped = 0

    for parts in rows:
        # Columns: igdb_id, store_ref, device, status, proton_version, app_version, notes, tested_at
        if len(parts) < 8:
            parts = parts + [""] * (8 - len(parts))
        (igdb_id, store_ref, device, status,
         proton_version, app_version, notes, tested_at) = [p.strip() for p in parts[:8]]

        if status not in VALID_STATUSES:
            print(f"  [compat] invalid status {status!r} — skip")
            skipped += 1
            continue
        if device and device not in VALID_DEVICES:
            print(f"  [compat] unknown device {device!r} — skip")
            skipped += 1
            continue
        device = device or "odin2"

        game_id = resolve_game_id(db, igdb_id, store_ref)
        if game_id is None:
            label = igdb_id or store_ref or "(no key)"
            print(f"  [compat] game not found: {label!r} — "
                  f"run importers first, or run merge.py for IGDB enrichment")
            skipped += 1
            continue

        db.execute("""
            INSERT INTO compat_notes
                (game_id, device, status, notes, proton_version, app_version, tested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (game_id, device, status,
              notes or None, proton_version or None,
              app_version or None, tested_at or None))
        upserted += 1

    db.commit()
    print(f"[compat-notes] done: {upserted} rows loaded, {skipped} skipped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tsv", type=pathlib.Path, default=DEFAULT_TSV)
    args = parser.parse_args()
    with open_db() as db:
        run(db, args.tsv)

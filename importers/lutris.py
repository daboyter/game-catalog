#!/usr/bin/env python3
"""
Lutris importer — reads ~/.local/share/lutris/pga.db via Python's sqlite3.

Note: sqlite3 CLI is not installed on Bazzite; Python's sqlite3 module is
the only way to read this database.

Two parts:
  1. Lutris's own games list (`games` table): the Battle.net / EA App /
     Ubisoft Connect launchers (kind=launcher) and plain Lutris games
     (store "lutris").
  2. The game libraries of the accounts signed in to those launchers
     (`service_games` table, synced by Lutris) as three stores of their own:
     battlenet, ea, ubisoft (LUTRIS_SERVICES). These play on bazzite through
     the Lutris launcher. A game Lutris has installed from one of them
     (`games` row with that service + service_id) marks that store row
     installed instead of becoming a separate "lutris" row, so nothing is
     counted twice; its Lutris playtime is kept.
     ownership.kind is "library" (in the account, however it got there:
     bought, Ubisoft+/Game Pass linking, Battle.net Call of Duty via Game
     Pass…), or free_claim for EA's Prime Gaming offers.
     The same game listed twice by a service (Steep, Trials Fusion; Diablo
     III under its Chinese name) is imported once.

Usage:
    ~/game-catalog-venv/bin/python3 importers/lutris.py
"""

import json, pathlib, re, sqlite3 as _sqlite3, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, upsert_game

LUTRIS_DB = pathlib.Path.home() / ".local/share/lutris/pga.db"

# Lutris service → our store name
LUTRIS_SERVICES = {"battlenet": "battlenet", "ea_app": "ea", "ubisoft": "ubisoft"}
TRADEMARKS = re.compile(r"[™®©]")


def _latin(name):
    """Share of letters that are ASCII: picks "Diablo III" over "暗黑破壞神III"."""
    letters = [c for c in name if c.isalpha()]
    return sum(c.isascii() for c in letters) / max(len(letters), 1)


def import_services(db, src, installed):
    """
    Account libraries from service_games → ownership rows in the battlenet/ea/
    ubisoft stores. installed: {(service, service_id): lutris games row}.
    Returns the set of (store, store_id) imported.
    """
    rows = src.execute("SELECT service, appid, name, slug, details FROM service_games "
                       "WHERE service IN (%s)" % ",".join("?" * len(LUTRIS_SERVICES)),
                       list(LUTRIS_SERVICES)).fetchall()
    # one per (service, slug): installed first, then the most Latin name
    best = {}
    for r in rows:
        key = (r["service"], r["slug"] or r["appid"])
        rank = ((r["service"], r["appid"]) in installed, _latin(r["name"] or ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)

    seen, counts = set(), {}
    for _, r in best.values():
        store = LUTRIS_SERVICES[r["service"]]
        title = TRADEMARKS.sub("", r["name"] or "").strip()
        if not title:
            continue
        game_id, _ = upsert_game(db, title)
        lg = installed.get((r["service"], r["appid"]))
        kind = "free_claim" if "prime-gaming" in (r["details"] or "") else "library"
        db.execute("""
            INSERT INTO ownership
                (game_id, store, store_id, store_title, kind, is_installed, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (store, store_id) DO UPDATE SET
                store_title  = excluded.store_title,
                is_installed = excluded.is_installed,
                raw_json     = excluded.raw_json,
                imported_at  = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (game_id, store, r["appid"], r["name"], kind, 1 if lg and lg["installed"] else 0,
              json.dumps({"service": r["service"], "appid": r["appid"], "slug": r["slug"],
                          "lutris_game": lg["slug"] if lg else None})))
        seen.add((store, r["appid"]))
        counts[store] = counts.get(store, 0) + 1
    print(f"  [lutris] account libraries: " +
          ", ".join(f"{s} {n}" for s, n in sorted(counts.items())) +
          f" ({len(rows) - len(best)} duplicate listings skipped)")
    return seen


def run(db):
    if not LUTRIS_DB.exists():
        print(f"  [lutris] {LUTRIS_DB} not found — skipping")
        return

    src = _sqlite3.connect(LUTRIS_DB)
    src.row_factory = _sqlite3.Row
    try:
        rows = src.execute("""
            SELECT name, runner, platform, playtime, lastplayed,
                   installed, year, service, service_id, slug
            FROM games
        """).fetchall()
    except _sqlite3.OperationalError as e:
        print(f"  [lutris] DB read error: {e}")
        src.close()
        return
    installed = {(r["service"], r["service_id"]): r for r in rows
                 if r["service"] in LUTRIS_SERVICES and r["service_id"]}
    try:
        seen, services_ok = import_services(db, src, installed), True
    except _sqlite3.OperationalError as e:      # older Lutris without service_games
        print(f"  [lutris] no account libraries: {e} — keeping the previous ones")
        seen, services_ok = set(), False
    src.close()

    # Known launcher names that are helpers, not games
    LAUNCHER_NAMES = {
        "ubisoft connect", "battle.net", "battlenet",
        "ea app", "origin", "epic games launcher",
        "gog galaxy", "amazon games", "xbox app",
    }

    print(f"  [lutris] {len(rows)} entries")
    new_games = upserted = playtime_rows = 0
    lutris_ids = set()                  # store_ids of the "lutris" rows written this run

    for r in rows:
        title = r["name"] or ""
        slug  = r["slug"] or ""
        if not title:
            continue

        # Classify launchers vs games
        is_launcher = title.lower().strip() in LAUNCHER_NAMES
        kind        = "launcher" if is_launcher else "purchased"
        is_inst     = 1 if r["installed"] else 0
        playtime_min = int(float(r["playtime"] or 0) * 60)
        last_ts      = int(r["lastplayed"] or 0)
        last_iso     = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_ts))
            if last_ts else None
        )

        service_store = LUTRIS_SERVICES.get(r["service"])
        if service_store and (service_store, r["service_id"]) in seen:
            # Installed from a launcher's library: already that store's row
            # (import_services); only its Lutris playtime is recorded below.
            game_id = db.execute("SELECT game_id FROM ownership WHERE store = ? AND store_id = ?",
                                 (service_store, r["service_id"])).fetchone()["game_id"]
            store_id = f"{service_store}:{r['service_id']}"
        else:
            game_id, created = upsert_game(db, TRADEMARKS.sub("", title).strip())
            if created:
                new_games += 1
            store_id = slug or title.lower().replace(" ", "-")
            lutris_ids.add(store_id)
            db.execute("""
                INSERT INTO ownership
                    (game_id, store, store_id, store_title, kind, is_installed, raw_json)
                VALUES (?, 'lutris', ?, ?, ?, ?, ?)
                ON CONFLICT (store, store_id) DO UPDATE SET
                    store_title  = excluded.store_title,
                    kind         = excluded.kind,
                    is_installed = excluded.is_installed,
                    raw_json     = excluded.raw_json,
                    imported_at  = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """, (game_id, store_id, title, kind, is_inst,
                  json.dumps({"runner": r["runner"], "service": r["service"],
                              "service_id": r["service_id"]})))
            upserted += 1

        if playtime_min > 0:
            db.execute("""
                INSERT INTO playtime (game_id, source, store_id, minutes_total, last_played_at)
                VALUES (?, 'lutris', ?, ?, ?)
                ON CONFLICT (game_id, source) DO UPDATE SET
                    minutes_total  = excluded.minutes_total,
                    last_played_at = excluded.last_played_at,
                    updated_at     = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """, (game_id, store_id, playtime_min, last_iso))
            playtime_rows += 1

    # Rows gone from Lutris — including, once, the launcher-library games that
    # used to be imported as "lutris" rows before they got stores of their own
    gone = [(r["id"], r["game_id"]) for r in db.execute(
        "SELECT id, game_id, store, store_id FROM ownership WHERE store IN ('lutris', %s)"
        % ",".join("?" * len(LUTRIS_SERVICES)), list(LUTRIS_SERVICES.values()))
        if (r["store"] == "lutris" and r["store_id"] not in lutris_ids)
        or (r["store"] != "lutris" and services_ok and (r["store"], r["store_id"]) not in seen)]
    for oid, gid in gone:
        db.execute("DELETE FROM ownership WHERE id = ?", (oid,))
        if not db.execute("SELECT 1 FROM ownership WHERE game_id = ?", (gid,)).fetchone():
            db.execute("DELETE FROM playtime WHERE game_id = ? AND source = 'lutris'", (gid,))

    db.commit()
    print(f"  [lutris] done: {new_games} new games, {upserted} Lutris rows, "
          f"{playtime_rows} playtime rows, {len(gone)} old rows removed")


if __name__ == "__main__":
    with open_db() as db:
        run(db)

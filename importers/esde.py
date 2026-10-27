#!/usr/bin/env python3
"""
ES-DE importer — parses all ~/ES-DE/gamelists/<system>/gamelist.xml files.

XML parser gotcha: 9 systems have an <alternativeEmulator> element BEFORE
<gameList>, making the file invalid XML (two root elements). We strip
everything outside <gameList>…</gameList> before parsing.

esde_entries rows are linked to games (game_id_fk) by normalized title match
if a matching game already exists in the games table, then corrected by IGDB
id (_db.link_esde_to_games; esde_entries.igdb_id comes from match_roms.py,
which also re-links after matching). Most ROM entries will have
game_id_fk = NULL — that is expected.

Stale entries: ES-DE keeps a game in gamelist.xml after its ROM file is
moved or deleted (it just hides it), so gamelists carried over from an
older setup can list hundreds of ROMs at paths that no longer exist (whole
systems that were merged into another folder, discs from before per-game
folders, and so on).
Only entries whose ROM file or folder exists under the ES-DE ROM directory
are imported, and rows for entries that are gone are deleted (except for
systems whose gamelist failed to parse, which keep their previous rows).

Entries ES-DE doesn't show as games are skipped too (hidden_from_esde):
  - disc files inside a multi-disc folder holding noload.txt: ES-DE lists only
    the .m3u next to the folder (PS1 "Final Fantasy IX (USA)/… (Disc 1).chd")
  - anything under a folder named *.off, a switched-off set (pico8 Imgs.off/,
    PNG copies of the .p8 cartridges)

Usage:
    ~/game-catalog-venv/bin/python3 importers/esde.py
"""

import pathlib, re, sys, xml.etree.ElementTree as ET
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from _db import open_db, normalize_title, find_game_id, link_esde_to_games

ESDE_DIR      = pathlib.Path.home() / "ES-DE"
GAMELISTS_DIR = ESDE_DIR / "gamelists"


def rom_directory() -> pathlib.Path:
    """ES-DE's ROMDirectory setting; empty (the default) means ~/ROMs."""
    settings = ESDE_DIR / "settings/es_settings.xml"
    if settings.exists():
        m = re.search(r'name="ROMDirectory" value="([^"]*)"', settings.read_text(errors="replace"))
        if m and m.group(1):
            return pathlib.Path(m.group(1).replace("~", str(pathlib.Path.home()), 1))
    return pathlib.Path.home() / "ROMs"


def rom_exists(roms_dir: pathlib.Path, system: str, rom_path: str) -> bool:
    """gamelist paths are relative to the system folder ("./Game (USA).zip") or absolute."""
    p = pathlib.Path(rom_path)
    if not p.is_absolute():
        p = roms_dir / system / rom_path.removeprefix("./")
    return p.exists()      # files, and folders ES-DE treats as one game (multi-disc)


def hidden_from_esde(roms_dir: pathlib.Path, system: str, rom_path: str) -> bool:
    """True for gamelist entries that aren't games of their own (see the docstring)."""
    parts = pathlib.PurePosixPath(rom_path.removeprefix("./")).parts
    folders = parts[:-1]
    if any(f.endswith(".off") for f in folders):
        return True
    return bool(folders) and (roms_dir / system / pathlib.Path(*folders) / "noload.txt").exists()


def parse_gamelist(path: pathlib.Path):
    raw   = path.read_text(errors="replace")
    start = raw.find("<gameList")
    end   = raw.rfind("</gameList>")
    if start == -1 or end == -1:
        return []
    tree = ET.fromstring(raw[start : end + len("</gameList>")])

    def t(el, tag):
        child = el.find(tag)
        return child.text.strip() if child is not None and child.text else None

    return [
        {
            "rom_path":      t(g, "path") or "",
            "es_name":       t(g, "name") or "",
            "genre":         t(g, "genre"),
            "developer":     t(g, "developer"),
            "publisher":     t(g, "publisher"),
            "players":       t(g, "players"),
            "rating":        float(t(g, "rating")) if t(g, "rating") else None,
            "release_date":  t(g, "releasedate"),
            "description":   t(g, "desc"),
            "favorite":      1 if t(g, "favorite") == "true" else 0,
            "playcount":     int(t(g, "playcount") or 0),
            "playtime_secs": int(t(g, "playtime") or 0),
            "lastplayed":    t(g, "lastplayed"),
        }
        for g in tree.findall("game")
    ]


def run(db):
    if not GAMELISTS_DIR.exists():
        print(f"  [esde] {GAMELISTS_DIR} not found — skipping")
        return

    systems = sorted(
        p for p in GAMELISTS_DIR.iterdir()
        if p.is_dir() and (p / "gamelist.xml").exists()
    )
    roms_dir = rom_directory()
    print(f"  [esde] {len(systems)} systems found, ROMs in {roms_dir}")

    total_entries = total_linked = total_stale = 0
    seen   = set()   # (system, rom_path) imported this run
    failed = set()   # systems whose gamelist couldn't be parsed: keep their old rows

    for system_dir in systems:
        system = system_dir.name
        try:
            entries = parse_gamelist(system_dir / "gamelist.xml")
        except ET.ParseError as e:
            print(f"  [esde/{system}] PARSE ERROR: {e} — skipping (previous rows kept)")
            failed.add(system)
            continue

        linked = stale = skipped = 0
        for e in entries:
            if not e["rom_path"] or not e["es_name"]:
                continue
            if not rom_exists(roms_dir, system, e["rom_path"]):
                stale += 1
                continue
            if hidden_from_esde(roms_dir, system, e["rom_path"]):
                skipped += 1
                continue
            seen.add((system, e["rom_path"]))

            game_id_fk = find_game_id(db, normalize_title(e["es_name"]))
            if game_id_fk:
                linked += 1

            db.execute("""
                INSERT INTO esde_entries
                    (game_id_fk, system, rom_path, es_name,
                     genre, developer, publisher, players, rating,
                     release_date, description,
                     favorite, playcount, playtime_secs, lastplayed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (system, rom_path) DO UPDATE SET
                    es_name       = excluded.es_name,
                    genre         = excluded.genre,
                    developer     = excluded.developer,
                    publisher     = excluded.publisher,
                    players       = excluded.players,
                    rating        = excluded.rating,
                    release_date  = excluded.release_date,
                    description   = excluded.description,
                    favorite      = excluded.favorite,
                    playcount     = excluded.playcount,
                    playtime_secs = excluded.playtime_secs,
                    lastplayed    = excluded.lastplayed,
                    game_id_fk    = excluded.game_id_fk,
                    imported_at   = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """, (
                game_id_fk, system, e["rom_path"], e["es_name"],
                e["genre"], e["developer"], e["publisher"], e["players"],
                e["rating"], e["release_date"], e["description"],
                e["favorite"], e["playcount"], e["playtime_secs"],
                e["lastplayed"],
            ))

        db.commit()
        total_entries += len(entries) - stale - skipped
        total_linked  += linked
        total_stale   += stale
        note = (f", {stale} skipped: ROM file missing" if stale else "") + \
               (f", {skipped} skipped: discs of a multi-disc game / switched-off folder" if skipped else "")
        print(f"  [esde/{system}] {len(entries) - stale - skipped} entries ({linked} linked to games{note})")

    # Remove rows for ROMs that are gone (or whose whole gamelist is gone)
    gone = [(r[0], r[1], r[2]) for r in db.execute("SELECT id, system, rom_path FROM esde_entries")
            if (r[1], r[2]) not in seen and r[1] not in failed]
    db.executemany("DELETE FROM esde_entries WHERE id = ?", [(g[0],) for g in gone])
    total_linked = link_esde_to_games(db)       # IGDB ids beat title matches
    db.commit()

    print(f"  [esde] done: {total_entries} total entries, {total_linked} linked to games, "
          f"{total_stale} stale gamelist entries skipped, {len(gone)} old rows removed")


if __name__ == "__main__":
    with open_db() as db:
        run(db)

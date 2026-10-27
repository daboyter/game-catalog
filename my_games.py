"""
The user's own play status and ratings — shared by the portal (which edits
them on game and ROM pages) and mcp_server.py (which only reads them).

Stored on bazzite in
    ~/.local/share/game-catalog/user/my-games.json
OUTSIDE the catalog database on purpose: library.db is derived data that a
refresh (or a rebuild from scratch) recreates, while this file is the
user's own and nothing in the refresh touches it. Every save keeps the
previous version as my-games.json.bak.

Keys survive rebuilds and re-matching, because none of them is a DB row id:
  store games  "game:igdb:<igdb_id>"            when IGDB-matched, else
               "game:store:<store>:<store_id>"  (e.g. game:store:steam:400)
  ES-DE ROMs   "rom:<system>:igdb:<igdb_id>"    when IGDB-matched, else
               "rom:<system>:path:<rom path>"   (one per ROM card, see roms.py)
Lookups try every key an item could have (its IGDB key, then each store
ref or ROM file), so an entry saved under a store ref is still found after
the game gets an IGDB match; the next save moves it to the IGDB key.

File format: {"version": 1, "entries": {key: entry}}, where an entry is
    {"kind": "game"|"rom", "title": ..., "status": "in_progress"|"complete"
     (optional), "rating": 1-10 (optional), "updated": ISO date, plus
     "igdb_id", "refs" (store refs) or "system"/"path" to help a human or
     Claude read the file}
An entry with neither status nor rating is removed.
"""

import datetime as dt, fcntl, json, os, pathlib, shutil, tempfile

MY_FILE   = pathlib.Path.home() / ".local/share/game-catalog/user/my-games.json"
LOCK_FILE = MY_FILE.with_name(".my-games.lock")

STATUSES = {"in_progress": "In progress", "complete": "Complete"}

_state = {"mtime": None, "entries": {}}


def game_keys(igdb_id, refs):
    """Keys a store game can be saved under, preferred first. refs: "store:store_id" strings."""
    keys = [f"game:igdb:{igdb_id}"] if igdb_id else []
    return keys + [f"game:store:{r}" for r in sorted(refs)]


def rom_keys(system, igdb_id, paths):
    """Keys a ROM card can be saved under, preferred first. paths: its files' gamelist paths."""
    keys = [f"rom:{system}:igdb:{igdb_id}"] if igdb_id else []
    return keys + [f"rom:{system}:path:{p}" for p in paths]


def _read():
    try:
        return json.loads(MY_FILE.read_text()).get("entries", {})
    except FileNotFoundError:
        return {}


def entries():
    """All entries {key: entry}; re-read only when the file changes."""
    try:
        mtime = MY_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    if mtime != _state["mtime"]:
        _state.update(mtime=mtime, entries=_read() if mtime else {})
    return _state["entries"]


def find(keys, data=None):
    """The entry saved under the first of `keys` that has one, else None."""
    data = entries() if data is None else data
    for k in keys:
        if k in data:
            return data[k]
    return None


_UNCHANGED = object()


def update(keys, *, kind, title, status=_UNCHANGED, rating=_UNCHANGED, **info):
    """
    Set status ("in_progress", "complete" or None to clear) and/or rating
    (1-10 or None to clear) for the item with `keys`; returns the new entry
    or None if it's now empty. Locked read-modify-write, atomic replace.
    """
    MY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()                                  # fresh, not the cached copy
        entry = dict(find(keys, data) or {})
        for k in keys:                                  # re-filed under the preferred key below
            data.pop(k, None)
        if status is not _UNCHANGED:
            entry.pop("status", None)
            if status:
                entry["status"] = status
        if rating is not _UNCHANGED:
            entry.pop("rating", None)
            if rating:
                entry["rating"] = rating
        if "status" in entry or "rating" in entry:
            entry.update(kind=kind, title=title, updated=dt.date.today().isoformat(),
                         **{k: v for k, v in info.items() if v})
            data[keys[0]] = entry
        else:
            entry = None
        if MY_FILE.exists():
            shutil.copy2(MY_FILE, MY_FILE.with_suffix(".json.bak"))
        fd, tmp = tempfile.mkstemp(dir=MY_FILE.parent, prefix=".my-games.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "entries": data}, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, MY_FILE)
    return entry
